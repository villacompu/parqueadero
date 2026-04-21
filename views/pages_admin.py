from __future__ import annotations


import os
import io
import zipfile
import tempfile
import shutil
import sqlite3
import pandas as pd
import streamlit as st
import json



from auth.auth_service import require_role, current_user, make_password
from db.core import db_connect, now_tz, DB_PATH, UPLOAD_DIR, ensure_dirs
from services.audit import audit
from services.config import get_config, set_config
from services.parking import normalize_plate, plate_is_valid, POLICY_PRESETS
from views.components import st_df




# -----------------------------
# Helpers locales (seguros)
# -----------------------------
def _table_has_column(con, table: str, col: str) -> bool:
    try:
        rows = con.execute(f"PRAGMA table_info({table})").fetchall()
        return any((r["name"] == col) for r in rows)
    except Exception:
        return False


def _extract_etapa_number(name: str) -> int | None:
    """
    Fallback si no existe etapa_zone o el nombre no trae etapa clara.
    Busca "Etapa 2" dentro del nombre.
    """
    try:
        import re

        m = re.search(r"Etapa\s*(\d+)", name or "", re.IGNORECASE)
        if m:
            return int(m.group(1))
    except Exception:
        pass
    return None


def _short_dt(s: str) -> str:
    try:
        ts = pd.to_datetime(s, errors="coerce")
        if pd.isna(ts):
            return str(s)[:16]
        return ts.strftime("%Y-%m-%dT%H:%M")
    except Exception:
        return str(s)[:16]


# -----------------------------
# ADMIN: Usuarios
# -----------------------------
def admin_users():
    require_role("ADMIN")
    con = db_connect()
    st.subheader("👤 Usuarios")

    with st.expander("Crear usuario", expanded=False):
        with st.form("cu"):
            username = st.text_input("Username", key="AUS_cu_user")
            full = st.text_input("Nombre", key="AUS_cu_name")
            role = st.selectbox("Rol", ["ADMIN", "PORTERO", "AUDITOR"], key="AUS_cu_role")
            pwd = st.text_input("Contraseña inicial", type="password", key="AUS_cu_pwd")
            ok = st.form_submit_button("Crear")
        if ok:
            if not username.strip() or not pwd:
                st.error("Username y contraseña son obligatorios.")
            else:
                ph, salt = make_password(pwd)
                try:
                    con.execute(
                        "INSERT INTO users(username,full_name,role,password_hash,salt_hex,is_active,created_at) VALUES (?,?,?,?,?,?,?)",
                        (username.strip(), full.strip() or None, role, ph, salt, 1, now_tz().isoformat()),
                    )
                    con.commit()
                    audit("ADMIN_CREATE_USER", current_user().id, {"username": username.strip(), "role": role})
                    st.success("Creado ✅")
                    st.rerun()
                except sqlite3.IntegrityError:
                    st.error("Ya existe ese username.")

    users = con.execute("SELECT id,username,full_name,role,is_active,created_at FROM users ORDER BY id DESC").fetchall()
    st.dataframe(pd.DataFrame([dict(r) for r in users]) if users else pd.DataFrame(), width="stretch", hide_index=True)

    st.markdown("### Editar usuario")
    if users:
        uid = st.selectbox(
            "Usuario",
            [int(r["id"]) for r in users],
            format_func=lambda i: next(x["username"] for x in users if int(x["id"]) == i),
            key="AUS_au_sel",
        )
        urow = next(x for x in users if int(x["id"]) == uid)

        col1, col2 = st.columns(2)
        with col1:
            new_pwd = st.text_input("Nueva contraseña", type="password", key="AUS_au_newpwd")
            if st.button("Resetear contraseña", key="AUS_au_reset"):
                if not new_pwd:
                    st.error("Ingresa contraseña.")
                else:
                    ph, salt = make_password(new_pwd)
                    con.execute("UPDATE users SET password_hash=?, salt_hex=? WHERE id=?", (ph, salt, uid))
                    con.commit()
                    audit("ADMIN_RESET_PASSWORD", current_user().id, {"user_id": uid})
                    st.success("Actualizada ✅")

        with col2:
            active = st.checkbox("Activo", value=bool(int(urow["is_active"])), key="AUS_au_active")
            if st.button("Guardar estado", key="AUS_au_save"):
                con.execute("UPDATE users SET is_active=? WHERE id=?", (1 if active else 0, uid))
                con.commit()
                audit("ADMIN_TOGGLE_USER", current_user().id, {"user_id": uid, "is_active": active})
                st.success("Listo ✅")
                st.rerun()

    con.close()


# -----------------------------
# ADMIN: Config
# -----------------------------
def admin_config():
    require_role("ADMIN")
    con = db_connect()
    u = current_user()
    assert u is not None

    st.subheader("⚙️ Configuración")

    # -------------------------
    # Límites residentes
    # -------------------------
    limits = get_config(con, "resident_limits", {"max_cars": 1, "max_motos": 2})
    with st.form("limits"):
        max_c = st.number_input("Max carros por apto", 0, 10, int(limits.get("max_cars", 1)), key="ACFG_cfg_maxc")
        max_m = st.number_input("Max motos por apto", 0, 10, int(limits.get("max_motos", 2)), key="ACFG_cfg_maxm")
        ok = st.form_submit_button("Guardar")
    if ok:
        set_config(con, "resident_limits", {"max_cars": int(max_c), "max_motos": int(max_m)})
        audit("ADMIN_SAVE_CONFIG", u.id, {"resident_limits": {"max_cars": int(max_c), "max_motos": int(max_m)}})
        st.success("Guardado ✅")

    # -------------------------
    # Dashboard público: título
    # -------------------------
    title = get_config(con, "public_dashboard_title", "Cupos de parqueadero")
    with st.form("ptitle"):
        t = st.text_input("Título dashboard público", value=title, key="ACFG_cfg_title")
        ok2 = st.form_submit_button("Guardar título")
    if ok2:
        set_config(con, "public_dashboard_title", t)
        audit("ADMIN_SAVE_CONFIG", u.id, {"public_dashboard_title": t})
        st.success("Guardado ✅")

    st.markdown("---")

    # -------------------------
    # Cierre del día: umbrales (minutos)
    # -------------------------
    warn_min = int(get_config(con, "end_day_threshold_warn_min", 60))
    crit_min = int(get_config(con, "end_day_threshold_crit_min", 180))

    st.markdown("### 🌙 Cierre del día · Umbrales de prioridad (minutos)")
    st.caption("Estos umbrales se usan para el semáforo 🟢🟠🔴 y para priorizar casos de visitantes sin salida.")

    with st.form("end_day_thresholds"):
        new_warn = st.number_input(
            "🟠 Advertencia desde (min)",
            min_value=1,
            max_value=10_000,
            value=warn_min,
            step=5,
            key="ACFG_end_warn_min",
        )
        new_crit = st.number_input(
            "🔴 Crítico desde (min)",
            min_value=1,
            max_value=10_000,
            value=crit_min,
            step=5,
            key="ACFG_end_crit_min",
        )
        ok3 = st.form_submit_button("Guardar umbrales")

    if ok3:
        if int(new_crit) <= int(new_warn):
            st.error("El umbral 🔴 Crítico debe ser MAYOR que el umbral 🟠 Advertencia.")
        else:
            set_config(con, "end_day_threshold_warn_min", int(new_warn))
            set_config(con, "end_day_threshold_crit_min", int(new_crit))
            audit("ADMIN_SAVE_CONFIG", u.id, {"end_day_threshold_warn_min": int(new_warn), "end_day_threshold_crit_min": int(new_crit)})
            st.success("Umbrales guardados ✅")

    st.markdown("---")

    # -------------------------
    # WhatsApp Auditor: plantilla preaviso incidencias
    # -------------------------
    st.markdown("### 📲 WhatsApp · Plantilla preaviso (Auditor → Incidencias)")
    st.caption("Esta plantilla se usa en el botón de WhatsApp dentro de Auditor → Incidencias.")

    default_tpl = (
        "Hola {resident_name}, te escribimos desde Administración / Auditoría de Primitiva Parque Natural.\n\n"
        "Se registró una *incidencia* asociada a tu apartamento Torre {tower_num}, Apto {apt_number}:\n"
        "• Placa: {plate}\n"
        "• Vehículo: {vehicle_type_text} ({vehicle_type})\n"
        "• Lugar/Zona: {place}\n"
        "• Fecha del reporte: {created_at}\n"
        "• Detalle: {description}\n\n"
        "Este mensaje es un *preaviso* (llamado de atención). Si este comportamiento se repite, podría convertirse en una *sanción* "
        "según el reglamento de la copropiedad.\n\n"
        "Por favor ayúdanos confirmando si el visitante ya salió y evitando que vuelva a ocurrir.\n"
        "¡Gracias!"
    )

    tpl = get_config(con, "auditor_incident_whatsapp_template", default_tpl)

    with st.expander("Ver variables disponibles", expanded=False):
        st.code(
            "{resident_name}\n{tower_num}\n{apt_number}\n{plate}\n{vehicle_type}\n{vehicle_type_text}\n{place}\n{created_at}\n{description}",
            language="text",
        )
        st.caption("Si quieres escribir llaves literales, usa {{ y }}.")

    with st.form("wa_tpl_form"):
        new_tpl = st.text_area("Plantilla", value=str(tpl), height=260, key="ACFG_inc_wa_tpl")
        ok4 = st.form_submit_button("Guardar plantilla")

    if ok4:
        set_config(con, "auditor_incident_whatsapp_template", new_tpl)
        audit("ADMIN_SAVE_CONFIG", u.id, {"auditor_incident_whatsapp_template": "(updated)"})
        st.success("Plantilla guardada ✅")

    with st.expander("Previsualizar ejemplo", expanded=False):
        sample_ctx = {
            "resident_name": "Juan Pérez",
            "tower_num": 3,
            "apt_number": "922",
            "plate": "ABC123",
            "vehicle_type": "CAR",
            "vehicle_type_text": "carro",
            "place": "Zona Visitantes Etapa 2 - Carros",
            "created_at": now_tz().strftime("%Y-%m-%dT%H:%M"),
            "description": "Visitante sin salida en cierre del día.",
        }
        try:
            preview = str(new_tpl if "new_tpl" in locals() else tpl).format_map(sample_ctx)
        except Exception:
            preview = "(La plantilla tiene un error de formato. Revisa llaves/variables.)"
        st.text(preview)

    st.markdown("---")

    # =========================================================
    # 🛠️ Mantenimiento y copias de seguridad (con papelera)
    # =========================================================
    st.markdown("## 🛠️ Mantenimiento y copias de seguridad")
    st.caption("Herramientas peligrosas. Antes de borrar o restaurar, descarga un respaldo.")

    # ---- Rutas (papelera) ----
    ensure_dirs()  # asegura UPLOAD_DIR exista

    UPLOAD_ABS = os.path.abspath(UPLOAD_DIR)
    DB_ABS = os.path.abspath(DB_PATH)

    UPLOAD_TRASH_DIR = UPLOAD_ABS + "_trash"  # ej: .../uploads_trash
    DB_TRASH_DIR = os.path.join(os.path.dirname(DB_ABS), "db_trash")

    def _trash_uploads(reason: str) -> tuple[int, str]:
        """Mueve archivos de uploads a una subcarpeta timestamp en uploads_trash/."""
        if not os.path.isdir(UPLOAD_ABS):
            return (0, "")

        os.makedirs(UPLOAD_TRASH_DIR, exist_ok=True)
        bucket = os.path.join(UPLOAD_TRASH_DIR, f"{reason}_{now_tz().strftime('%Y%m%d_%H%M%S')}")
        os.makedirs(bucket, exist_ok=True)

        moved = 0
        for fn in os.listdir(UPLOAD_ABS):
            src = os.path.join(UPLOAD_ABS, fn)
            if not os.path.isfile(src):
                continue
            try:
                shutil.move(src, os.path.join(bucket, fn))
                moved += 1
            except Exception:
                pass

        return (moved, bucket)

    def _trash_db(reason: str) -> str:
        """Mueve el DB actual a db_trash/ antes de restaurar."""
        if not os.path.isfile(DB_ABS):
            return ""
        os.makedirs(DB_TRASH_DIR, exist_ok=True)
        bucket = os.path.join(DB_TRASH_DIR, f"{reason}_{now_tz().strftime('%Y%m%d_%H%M%S')}")
        os.makedirs(bucket, exist_ok=True)
        dst = os.path.join(bucket, os.path.basename(DB_ABS))
        try:
            shutil.move(DB_ABS, dst)
            return dst
        except Exception:
            return ""

    # ---------- BACKUPS + RESTORE ----------
    with st.expander("📦 Copias de seguridad / Restaurar", expanded=False):
        st.caption("Recomendado: descargar el SQLite completo. Importante: las evidencias (fotos) están en carpeta uploads (se respaldan aparte).")
        st.caption(f"DB: `{DB_ABS}`")
        st.caption(f"Evidencias: `{UPLOAD_ABS}`")

        # 1) Backup SQLite completo
        if st.button("Preparar respaldo SQLite", key="ACFG_bk_sqlite_prepare"):
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".sqlite3")
            tmp_path = tmp.name
            tmp.close()

            src = sqlite3.connect(DB_ABS)
            dst = sqlite3.connect(tmp_path)
            try:
                src.backup(dst)
            finally:
                dst.close()
                src.close()

            with open(tmp_path, "rb") as f:
                st.session_state["_ACFG_sqlite_backup_bytes"] = f.read()
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

        bk = st.session_state.get("_ACFG_sqlite_backup_bytes")
        if bk:
            st.download_button(
                "⬇️ Descargar respaldo SQLite",
                data=bk,
                file_name=f"parking_ph_backup_{now_tz().strftime('%Y%m%d_%H%M')}.sqlite3",
                mime="application/octet-stream",
                key="ACFG_bk_sqlite_download",
            )

        st.markdown("---")

        # 2) Backup evidencias (uploads) como ZIP
        if st.button("Preparar ZIP evidencias (uploads)", key="ACFG_bk_uploads_prepare"):
            bio = io.BytesIO()
            with zipfile.ZipFile(bio, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                if os.path.isdir(UPLOAD_ABS):
                    for fn in os.listdir(UPLOAD_ABS):
                        p = os.path.join(UPLOAD_ABS, fn)
                        if os.path.isfile(p):
                            zf.write(p, arcname=fn)
            st.session_state["_ACFG_uploads_zip_bytes"] = bio.getvalue()

        up_zip = st.session_state.get("_ACFG_uploads_zip_bytes")
        if up_zip:
            st.download_button(
                "⬇️ Descargar ZIP evidencias (uploads)",
                data=up_zip,
                file_name=f"parking_ph_uploads_{now_tz().strftime('%Y%m%d_%H%M')}.zip",
                mime="application/zip",
                key="ACFG_bk_uploads_download",
            )

        st.markdown("---")

        # 3) Exportar tablas a CSV (ZIP)
        st.caption("Exportar tablas a CSV (ZIP). Útil para revisar o migrar.")
        col1, col2, col3 = st.columns(3)
        with col1:
            inc_infra = st.checkbox("Infraestructura", value=True, key="ACFG_bk_infra")
            inc_users = st.checkbox("Usuarios", value=True, key="ACFG_bk_users")
        with col2:
            inc_oper = st.checkbox("Operación (tickets)", value=False, key="ACFG_bk_oper")
            inc_inc = st.checkbox("Incidencias", value=False, key="ACFG_bk_inc")
        with col3:
            inc_san = st.checkbox("Sanciones", value=False, key="ACFG_bk_san")
            inc_audit = st.checkbox("Auditoría (audit_log)", value=False, key="ACFG_bk_audit")

        table_groups = []
        if inc_infra:
            table_groups += ["towers", "apartments", "zones", "zone_tower", "private_cells", "resident_vehicles"]
        if inc_users:
            table_groups += ["users"]
        if inc_oper:
            table_groups += ["tickets"]
        if inc_inc:
            table_groups += ["incidents"]
        if inc_san:
            table_groups += ["sanctions"]
        if inc_audit:
            table_groups += ["audit_log"]
        table_groups += ["config"]
        table_groups = list(dict.fromkeys(table_groups))

        if st.button("Preparar ZIP (CSV)", key="ACFG_bk_zip_prepare"):
            bio = io.BytesIO()
            with zipfile.ZipFile(bio, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                for tname in table_groups:
                    try:
                        df = pd.read_sql_query(f"SELECT * FROM {tname}", con)
                    except Exception:
                        continue
                    zf.writestr(f"{tname}.csv", df.to_csv(index=False, encoding="utf-8"))
            st.session_state["_ACFG_zip_backup_bytes"] = bio.getvalue()

        zbytes = st.session_state.get("_ACFG_zip_backup_bytes")
        if zbytes:
            st.download_button(
                "⬇️ Descargar ZIP (CSV)",
                data=zbytes,
                file_name=f"parking_ph_export_{now_tz().strftime('%Y%m%d_%H%M')}.zip",
                mime="application/zip",
                key="ACFG_bk_zip_download",
            )

        st.markdown("---")

        # 4) Restaurar DB desde respaldo (⚠️)
        st.markdown("#### ♻️ Restaurar base de datos (SQLite)")
        st.warning("Esto reemplaza la base de datos actual. Se guarda una copia de la DB anterior en `db_trash/`.")
        restore_db = st.file_uploader("Subir respaldo SQLite (.sqlite3)", type=["sqlite3", "db", "bd"], key="ACFG_restore_db_file")
        confirm_restore = st.text_input("Escribe: RESTAURAR", value="", key="ACFG_restore_db_confirm")
        can_restore = (confirm_restore.strip().upper() == "RESTAURAR")

        if st.button("♻️ Restaurar DB ahora", disabled=(not can_restore or restore_db is None), key="ACFG_restore_db_btn"):
            # Cerramos conexión antes de reemplazar archivo (importante en Windows)
            con.close()

            old_path = _trash_db("before_restore")
            if not old_path:
                st.warning("No se pudo mover la DB anterior a papelera (puede estar en uso). Intenta cerrar la app y volver a intentar.")
                st.stop()

            try:
                with open(DB_ABS, "wb") as f:
                    f.write(restore_db.getbuffer())
            except Exception as e:
                st.error("No se pudo escribir la DB restaurada.")
                st.exception(e)
                st.stop()

            audit("ADMIN_RESTORE_DB", u.id, {"old_db": old_path})
            st.success("DB restaurada ✅. Reiniciando pantalla…")
            st.rerun()

        st.markdown("---")

        # 5) Restaurar evidencias (uploads) desde ZIP (opcional)
        st.markdown("#### ♻️ Restaurar evidencias (uploads)")
        st.caption("Esto extrae archivos dentro de la carpeta uploads. Si existe un archivo con el mismo nombre, lo renombra con sufijo.")
        restore_up = st.file_uploader("Subir ZIP de evidencias (uploads)", type=["zip"], key="ACFG_restore_uploads_file")
        confirm_up = st.text_input("Escribe: SUBIR", value="", key="ACFG_restore_uploads_confirm")
        can_up = (confirm_up.strip().upper() == "SUBIR")

        if st.button("♻️ Restaurar evidencias ahora", disabled=(not can_up or restore_up is None), key="ACFG_restore_uploads_btn"):
            ensure_dirs()
            os.makedirs(UPLOAD_ABS, exist_ok=True)

            try:
                bio = io.BytesIO(restore_up.getbuffer())
                with zipfile.ZipFile(bio, "r") as zf:
                    for member in zf.namelist():
                        fn = os.path.basename(member)
                        if not fn:
                            continue
                        out = os.path.join(UPLOAD_ABS, fn)
                        if os.path.exists(out):
                            base, ext = os.path.splitext(fn)
                            out = os.path.join(UPLOAD_ABS, f"{base}_restored_{now_tz().strftime('%Y%m%d_%H%M%S')}{ext}")
                        with zf.open(member) as src_f, open(out, "wb") as dst_f:
                            dst_f.write(src_f.read())

                audit("ADMIN_RESTORE_UPLOADS", u.id, {})
                st.success("Evidencias restauradas ✅")
            except Exception as e:
                st.error("No se pudo restaurar evidencias.")
                st.exception(e)

    st.markdown("---")

    # ---------- RESET / PURGE ----------
    with st.expander("🧨 Reinicios / limpieza de datos (ADMIN)", expanded=False):
        st.warning(
            "Estas acciones borran información. **Antes** descarga un respaldo. "
            "Las evidencias NO se eliminan: se mueven a una **papelera**."
        )

        confirm = st.text_input("Escribe: BORRAR", value="", key="ACFG_reset_confirm")
        can = (confirm.strip().upper() == "BORRAR")

        colA, colB = st.columns(2)

        with colA:
            st.markdown("**Reinicios rápidos**")

            if st.button("🧹 Reiniciar estadísticas (audit_log)", disabled=not can, key="ACFG_reset_audit"):
                con.execute("DELETE FROM audit_log")
                try:
                    con.execute("DELETE FROM sqlite_sequence WHERE name='audit_log'")
                except Exception:
                    pass
                con.commit()
                audit("ADMIN_RESET_AUDIT_LOG", u.id, {})
                st.success("Audit log reiniciado ✅")
                st.rerun()

            if st.button("🧯 Reiniciar incidencias", disabled=not can, key="ACFG_reset_inc"):
                con.execute("DELETE FROM incidents")
                try:
                    con.execute("DELETE FROM sqlite_sequence WHERE name='incidents'")
                except Exception:
                    pass
                con.commit()
                audit("ADMIN_RESET_INCIDENTS", u.id, {})
                st.success("Incidencias reiniciadas ✅")
                st.rerun()

            if st.button("🧾 Reiniciar sanciones", disabled=not can, key="ACFG_reset_san"):
                # mover evidencias a papelera (no se eliminan)
                moved, bucket = _trash_uploads("sanctions_reset")
                if moved > 0:
                    st.info(f"📦 Evidencias movidas a papelera: {moved} archivo(s) · {bucket}")

                con.execute("DELETE FROM sanctions")
                try:
                    con.execute("DELETE FROM sqlite_sequence WHERE name='sanctions'")
                except Exception:
                    pass
                con.commit()

                audit("ADMIN_RESET_SANCTIONS", u.id, {"uploads_moved": moved})
                st.success("Sanciones reiniciadas ✅")
                st.rerun()

        with colB:
            st.markdown("**Reinicio operación**")
            st.caption("Borra tickets + incidencias + sanciones + auditoría. Mantiene infraestructura.")

            if st.button("🧹 Reiniciar operación (tickets + incidencias + sanciones + auditoría)", disabled=not can, key="ACFG_reset_ops"):
                for tname in ["tickets", "incidents", "sanctions", "audit_log"]:
                    con.execute(f"DELETE FROM {tname}")
                    try:
                        con.execute("DELETE FROM sqlite_sequence WHERE name=?", (tname,))
                    except Exception:
                        pass
                con.commit()

                moved, bucket = _trash_uploads("operation_reset")
                if moved > 0:
                    st.info(f"📦 Evidencias movidas a papelera: {moved} archivo(s) · {bucket}")

                audit("ADMIN_RESET_OPERATION", u.id, {"uploads_moved": moved})
                st.success("Operación reiniciada ✅")
                st.rerun()

            st.markdown("---")
            st.caption("Dejar SOLO admin/portero1/auditor1 con contraseñas por defecto (admin123/portero123/auditor123).")

            if st.button("👤 Restablecer usuarios por defecto", disabled=not can, key="ACFG_reset_users"):
                con.execute("DELETE FROM users WHERE username NOT IN ('admin','portero1','auditor1')")

                def _upsert_user(username: str, full_name: str, role: str, pwd: str):
                    ph, salt = make_password(pwd)
                    ex = con.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
                    if ex:
                        con.execute(
                            "UPDATE users SET full_name=?, role=?, password_hash=?, salt_hex=?, is_active=1 WHERE username=?",
                            (full_name, role, ph, salt, username),
                        )
                    else:
                        con.execute(
                            "INSERT INTO users(username,full_name,role,password_hash,salt_hex,is_active,created_at) VALUES (?,?,?,?,?,?,?)",
                            (username, full_name, role, ph, salt, 1, now_tz().isoformat()),
                        )

                _upsert_user("admin", "Administrador", "ADMIN", "admin123")
                _upsert_user("portero1", "Portero 1", "PORTERO", "portero123")
                _upsert_user("auditor1", "Auditor 1", "AUDITOR", "auditor123")

                con.commit()
                audit("ADMIN_RESET_USERS_DEFAULT", u.id, {})
                st.success("Usuarios restablecidos ✅")
                st.rerun()

            st.markdown("---")
            st.caption("Opcional: limpiar también vehículos residentes (NO es infraestructura).")

            if st.button("🚘 Limpiar vehículos residentes", disabled=not can, key="ACFG_reset_rv"):
                con.execute("DELETE FROM resident_vehicles")
                try:
                    con.execute("DELETE FROM sqlite_sequence WHERE name='resident_vehicles'")
                except Exception:
                    pass
                con.commit()
                audit("ADMIN_RESET_RESIDENT_VEHICLES", u.id, {})
                st.success("Vehículos residentes eliminados ✅")
                st.rerun()

        st.markdown("---")
        st.caption("Después de borrar mucho, puedes optimizar el archivo DB (VACUUM).")

        if st.button("🧽 Optimizar DB (VACUUM)", disabled=not can, key="ACFG_vacuum"):
            con.commit()
            con.execute("VACUUM")
            st.success("DB optimizada ✅")

    con.close()


# -----------------------------
# ADMIN: Estructura
# -----------------------------
def admin_structure():
    require_role("ADMIN")
    con = db_connect()
    st.subheader("🏗️ Estructura")

    zones_has_etapa = _table_has_column(con, "zones", "etapa_zone")

    tab1, tab2, tab3, tab_cells, tab_veh, tab4 = st.tabs(
        ["Torres", "Apartamentos", "Zonas", "Celdas", "Vehículos residentes", "Zonas ↔ Torres (cercanía)"]
    )

    # -------------------------
    # TORRES
    # -------------------------
    with tab1:
        st.markdown("### Crear torre ")

        with st.form("ct_create"):
            tower_num = st.number_input("Número torre", 1, 999, 1, key="ASTR_ct_num")
            etapa = st.number_input("Etapa residencial", 1, 20, 1, key="ASTR_ct_et")
            ok = st.form_submit_button("Crear torre")

        if ok:
            exists = con.execute("SELECT id FROM towers WHERE tower_num=?", (int(tower_num),)).fetchone()
            if exists:
                st.warning(f"La **Torre {int(tower_num)}** ya existe. Edita abajo.")
            else:
                con.execute("INSERT INTO towers(tower_num,etapa_residencial,is_active) VALUES (?,?,1)", (int(tower_num), int(etapa)))
                con.commit()
                audit("ADMIN_CREATE_TOWER", current_user().id, {"tower_num": int(tower_num), "etapa": int(etapa)})
                st.success("Torre creada ✅")
                st.rerun()

        towers = con.execute("SELECT id, tower_num, etapa_residencial, is_active FROM towers ORDER BY tower_num").fetchall()

        if towers:
            active = [t for t in towers if int(t["is_active"]) == 1]
            inactive = [t for t in towers if int(t["is_active"]) == 0]
            etapas = sorted({int(t["etapa_residencial"]) for t in active}) if active else []

            c1, c2, c3 = st.columns(3)
            c1.metric("Torres activas", len(active))
            c2.metric("Torres inactivas", len(inactive))
            c3.metric("Etapas", ", ".join(map(str, etapas)) if etapas else "-")

            st.markdown("#### Torres por etapa (activas)")
            stage_counts = {}
            for t in active:
                e = int(t["etapa_residencial"])
                stage_counts[e] = stage_counts.get(e, 0) + 1
            df_stage = pd.DataFrame([{"Etapa": e, "Torres": n} for e, n in sorted(stage_counts.items(), key=lambda x: x[0])])
            st.dataframe(df_stage, width="stretch", hide_index=True)

            st.markdown("#### Lista rápida")
            lines = []
            for t in towers:
                status = "✅" if int(t["is_active"]) == 1 else "⛔"
                lines.append(f"- {status} **Torre {int(t['tower_num'])}** — Etapa {int(t['etapa_residencial'])}")
            st.markdown("\n".join(lines))
        else:
            st.info("Aún no hay torres creadas.")

        st.markdown("### Editar torre")
        if towers:
            tid = st.selectbox(
                "Torre",
                [int(r["id"]) for r in towers],
                format_func=lambda i: f"Torre {next(x['tower_num'] for x in towers if int(x['id'])==i)}",
                key="ASTR_tw_sel",
            )
            tr = next(x for x in towers if int(x["id"]) == tid)
            k = f"tw_{tid}"

            new_num = st.number_input("Número torre", 1, 999, int(tr["tower_num"]), key=f"{k}_num")
            new_et = st.number_input("Etapa", 1, 20, int(tr["etapa_residencial"]), key=f"{k}_et")
            active_flag = st.checkbox("Activa", value=bool(int(tr["is_active"])), key=f"{k}_act")

            if st.button("Actualizar torre", key=f"{k}_save"):
                try:
                    con.execute("UPDATE towers SET tower_num=?, etapa_residencial=?, is_active=? WHERE id=?", (int(new_num), int(new_et), 1 if active_flag else 0, tid))
                    con.commit()
                    audit("ADMIN_UPDATE_TOWER", current_user().id, {"id": tid, "tower_num": int(new_num), "etapa": int(new_et), "is_active": active_flag})
                    st.success("Actualizada ✅")
                    st.rerun()
                except sqlite3.IntegrityError:
                    st.error("Ya existe otra torre con ese número.")

    # -------------------------
    # APARTAMENTOS
    # -------------------------
    with tab2:
        st.markdown("### Crear apartamento ")

        towers_active = con.execute("SELECT * FROM towers WHERE is_active=1 ORDER BY tower_num").fetchall()
        if not towers_active:
            st.warning("Primero crea torres activas.")
        else:
            tower_choice = st.selectbox(
                "Torre",
                [int(t["id"]) for t in towers_active],
                format_func=lambda i: f"Torre {next(x['tower_num'] for x in towers_active if int(x['id'])==i)}",
                key="ASTR_apt_create_tower_choice",
            )

            # ⚠️ Importante: este checkbox debe estar FUERA del st.form.
            # En Streamlit, los widgets dentro de un formulario NO disparan rerun hasta que se envía el formulario.
            # Por eso, al marcar "Apartamento arrendado" no se mostraban inmediatamente los campos extra.
            is_rented = st.checkbox(
                "Apartamento arrendado",
                value=bool(st.session_state.get("ASTR_apt_create_rented", False)),
                key="ASTR_apt_create_rented",
                help="Si está arrendado, guarda datos del inquilino (contacto) y del dueño (notificaciones).",
            )

            with st.form("ca_create"):
                apt_number = st.text_input("Apartamento", placeholder="922", key="ASTR_apt_create_num")
                # (el checkbox está afuera para que los campos aparezcan al instante)

                if is_rented:
                    st.caption("Arrendado: guarda datos del **inquilino (residente)** y del **dueño**.")
                    tenant_name = st.text_input("Nombre del inquilino (residente)", key="ASTR_apt_create_tenant_name")
                    tenant_whatsapp = st.text_input("WhatsApp del inquilino", placeholder="3001234567", key="ASTR_apt_create_tenant_wa")
                    owner_name = st.text_input("Nombre del dueño", key="ASTR_apt_create_owner_name")
                    owner_whatsapp = st.text_input("WhatsApp del dueño", placeholder="3001234567", key="ASTR_apt_create_owner_wa")
                else:
                    st.caption("No arrendado: el **residente es el dueño**.")
                    resident_name = st.text_input("Nombre del residente (dueño)", key="ASTR_apt_create_name")
                    whatsapp = st.text_input("WhatsApp del residente", placeholder="3001234567", key="ASTR_apt_create_wa")
                    tenant_name = None
                    tenant_whatsapp = None
                    owner_name = None
                    owner_whatsapp = None

                has_private = st.checkbox("Tiene parqueadero privado", value=False, key="ASTR_apt_create_priv")
                notes = st.text_input("Notas (opcional)", key="ASTR_apt_create_notes")
                ok = st.form_submit_button("Crear apartamento")

            if ok:
                if not apt_number.strip():
                    st.error("Apartamento es obligatorio.")
                else:
                    exists = con.execute(
                        "SELECT id FROM apartments WHERE tower_id=? AND apt_number=?",
                        (int(tower_choice), apt_number.strip()),
                    ).fetchone()

                    if exists:
                        tnum = next(x["tower_num"] for x in towers_active if int(x["id"]) == int(tower_choice))
                        st.warning(f"El apartamento **T{tnum}-{apt_number.strip()}** ya existe. Edita abajo.")
                    else:
                        # Para compatibilidad: resident_name/whatsapp siempre representan a quien vive (contacto portería).
                        if is_rented:
                            res_name_db = (tenant_name or "").strip() or None
                            wa_db = (tenant_whatsapp or "").strip() or None
                            owner_name_db = (owner_name or "").strip() or None
                            owner_wa_db = (owner_whatsapp or "").strip() or None
                            tenant_name_db = res_name_db
                            tenant_wa_db = wa_db
                        else:
                            res_name_db = (resident_name or "").strip() or None
                            wa_db = (whatsapp or "").strip() or None
                            owner_name_db = res_name_db
                            owner_wa_db = wa_db
                            tenant_name_db = None
                            tenant_wa_db = None

                        con.execute(
                            """
                            INSERT INTO apartments(
                                tower_id, apt_number,
                                resident_name, whatsapp,
                                has_private_parking, notes, is_active,
                                is_rented,
                                owner_name, owner_whatsapp,
                                tenant_name, tenant_whatsapp
                            )
                            VALUES (?,?,?,?,?,?,1,?,?,?,?,?)
                            """,
                            (
                                int(tower_choice),
                                apt_number.strip(),
                                res_name_db,
                                wa_db,
                                1 if has_private else 0,
                                notes.strip() or None,
                                1 if is_rented else 0,
                                owner_name_db,
                                owner_wa_db,
                                tenant_name_db,
                                tenant_wa_db,
                            ),
                        )
                        con.commit()
                        audit("ADMIN_CREATE_APT", current_user().id, {"tower_id": int(tower_choice), "apt": apt_number.strip(), "is_rented": bool(is_rented)})
                        st.success("Apartamento creado ✅")
                        st.rerun()

        # -------------------------
        # Carga masiva por CSV (apartamentos)
        # -------------------------
        with st.expander("📥 Carga masiva (CSV) — Apartamentos", expanded=False):
            import io  # asegúrate de tenerlo; aquí lo incluimos para que no falle

            st.caption(
                "Sube un CSV para crear o actualizar apartamentos. "
                "Tip: primero crea torres. Si un apartamento ya existe, puedes elegir si actualizarlo."
            )

            # ✅ Mostrar último resultado (persistente incluso después de st.rerun)
            last = st.session_state.get("_ASTR_apt_csv_last_result")
            if last:
                st.success(
                    "Última carga ✅  ·  "
                    f"creados: {last.get('created', 0)} · actualizados: {last.get('updated', 0)} · "
                    f"omitidos: {last.get('skipped', 0)} · errores: {last.get('errors', 0)}"
                )
                notes_last = last.get("notes") or []
                if notes_last:
                    with st.expander("Ver detalles (última carga)", expanded=False):
                        for line in notes_last[:80]:
                            st.write("-", line)

            sample = pd.DataFrame(
                [
                    {
                        "tower_num": 3,
                        "apt_number": "922",
                        "is_rented": 0,
                        "resident_name": "Juan Pérez",
                        "whatsapp": "3001234567",
                        "owner_name": "",
                        "owner_whatsapp": "",
                        "tenant_name": "",
                        "tenant_whatsapp": "",
                        "has_private_parking": 0,
                        "notes": "",
                        "is_active": 1,
                    },
                    {
                        "tower_num": 4,
                        "apt_number": "101",
                        "is_rented": 1,
                        "resident_name": "(opcional)",
                        "whatsapp": "(opcional)",
                        "owner_name": "María Dueña",
                        "owner_whatsapp": "3010000000",
                        "tenant_name": "Carlos Inquilino",
                        "tenant_whatsapp": "3020000000",
                        "has_private_parking": 1,
                        "notes": "Arrendado",
                        "is_active": 1,
                    },
                ]
            )

            st.download_button(
                "⬇️ Descargar plantilla CSV (apartamentos)",
                data=sample.to_csv(index=False, sep=";").encode("utf-8-sig"),
                file_name="plantilla_apartamentos.csv",
                mime="text/csv",
                key="ASTR_apt_csv_tpl",
            )

            upsert = st.checkbox("Si existe, actualizar", value=True, key="ASTR_apt_csv_upsert")
            f = st.file_uploader("Subir CSV de apartamentos", type=["csv"], key="ASTR_apt_csv_file")

            if f is not None:
                # -------- leer CSV robusto --------
                df_in = None
                try:
                    raw = f.getvalue()

                    text = None
                    for enc in ("utf-8-sig", "utf-8", "latin1"):
                        try:
                            text = raw.decode(enc)
                            break
                        except Exception:
                            pass

                    if text is None:
                        st.error("No pude leer el archivo. Exporta como CSV UTF-8.")
                        st.stop()

                    df_in = pd.read_csv(
                        io.StringIO(text),
                        sep=None,  # auto detecta , o ;
                        engine="python",
                        dtype=str,
                        keep_default_na=False,
                    )
                    df_in.columns = [c.strip() for c in df_in.columns]

                except Exception as e:
                    st.error("No pude leer el CSV. Verifica separador y encoding.")
                    st.exception(e)
                    df_in = None

                if df_in is not None:
                    st.dataframe(df_in.head(30), width="stretch", hide_index=True)

                    def _to_bool(x, default=0) -> int:
                        if x is None or (isinstance(x, float) and pd.isna(x)):
                            return int(default)
                        s = str(x).strip().lower()
                        if s in ("1", "true", "t", "si", "sí", "yes", "y"):
                            return 1
                        if s in ("0", "false", "f", "no", "n"):
                            return 0
                        try:
                            return int(s)
                        except Exception:
                            return int(default)

                    if st.button("Procesar CSV (apartamentos)", key="ASTR_apt_csv_run", type="primary"):
                        towers_map = {
                            int(t["tower_num"]): int(t["id"])
                            for t in con.execute("SELECT id,tower_num FROM towers").fetchall()
                        }

                        created = 0
                        updated = 0
                        skipped = 0
                        errors = 0
                        notes = []  # ✅ para ver detalles después

                        for i, r in df_in.iterrows():
                            try:
                                # ---- básicos ----
                                tower_raw = str(r.get("tower_num") or "").strip()
                                apt_num = str(r.get("apt_number") or "").strip()

                                if not tower_raw or not apt_num:
                                    skipped += 1
                                    notes.append(f"Fila {i+1}: tower_num/apt_number vacío → omitida")
                                    continue

                                try:
                                    tower_num = int(tower_raw)
                                except Exception:
                                    skipped += 1
                                    notes.append(f"Fila {i+1}: tower_num inválido '{tower_raw}' → omitida")
                                    continue

                                tower_id = towers_map.get(tower_num)
                                if not tower_id:
                                    skipped += 1
                                    notes.append(f"Fila {i+1}: torre {tower_num} no existe → omitida")
                                    continue

                                is_rented = _to_bool(r.get("is_rented", 0), 0)

                                # contacto portería (quien vive / a quién llamar)
                                res_name = str(r.get("resident_name") or "").strip()
                                res_wa = str(r.get("whatsapp") or "").strip()

                                tenant_name = str(r.get("tenant_name") or "").strip()
                                tenant_wa = str(r.get("tenant_whatsapp") or "").strip()

                                owner_name = str(r.get("owner_name") or "").strip()
                                owner_wa = str(r.get("owner_whatsapp") or "").strip()

                                # ✅ contacto (resident_* tiene prioridad; si viene vacío, usa tenant_*)
                                contact_name = res_name or tenant_name
                                contact_wa = res_wa or tenant_wa

                                if is_rented == 1:
                                    # Arrendado: el dueño se toma del CSV (si viene)
                                    owner_name_db = owner_name or ""
                                    owner_wa_db = owner_wa or ""

                                    # Inquilino: se toma del CSV; si viene vacío, usa contact
                                    tenant_name_db = tenant_name or contact_name or ""
                                    tenant_wa_db = tenant_wa or contact_wa or ""
                                else:
                                    # No arrendado: dueño = contacto / residente
                                    owner_name_db = contact_name or ""
                                    owner_wa_db = contact_wa or ""
                                    tenant_name_db = ""
                                    tenant_wa_db = ""

                                has_private = _to_bool(r.get("has_private_parking", 0), 0)
                                is_active = _to_bool(r.get("is_active", 1), 1)
                                notes_db = str(r.get("notes") or "").strip() or None

                                # ---- INSERT / UPDATE ----
                                if upsert:
                                    try:
                                        con.execute(
                                            """
                                            INSERT INTO apartments(
                                                tower_id, apt_number,
                                                resident_name, whatsapp,
                                                is_rented, owner_name, owner_whatsapp,
                                                tenant_name, tenant_whatsapp,
                                                has_private_parking, notes, is_active
                                            )
                                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                                            """,
                                            (
                                                int(tower_id),
                                                apt_num,
                                                contact_name or None,
                                                contact_wa or None,
                                                int(is_rented),
                                                owner_name_db or None,
                                                owner_wa_db or None,
                                                tenant_name_db or None,
                                                tenant_wa_db or None,
                                                int(has_private),
                                                notes_db,
                                                int(is_active),
                                            ),
                                        )
                                        created += 1
                                    except sqlite3.IntegrityError:
                                        con.execute(
                                            """
                                            UPDATE apartments
                                            SET resident_name=?, whatsapp=?,
                                                is_rented=?, owner_name=?, owner_whatsapp=?,
                                                tenant_name=?, tenant_whatsapp=?,
                                                has_private_parking=?, notes=?, is_active=?
                                            WHERE tower_id=? AND apt_number=?
                                            """,
                                            (
                                                contact_name or None,
                                                contact_wa or None,
                                                int(is_rented),
                                                owner_name_db or None,
                                                owner_wa_db or None,
                                                tenant_name_db or None,
                                                tenant_wa_db or None,
                                                int(has_private),
                                                notes_db,
                                                int(is_active),
                                                int(tower_id),
                                                apt_num,
                                            ),
                                        )
                                        updated += 1
                                else:
                                    # solo crear
                                    try:
                                        con.execute(
                                            """
                                            INSERT INTO apartments(
                                                tower_id, apt_number,
                                                resident_name, whatsapp,
                                                is_rented, owner_name, owner_whatsapp,
                                                tenant_name, tenant_whatsapp,
                                                has_private_parking, notes, is_active
                                            )
                                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                                            """,
                                            (
                                                int(tower_id),
                                                apt_num,
                                                contact_name or None,
                                                contact_wa or None,
                                                int(is_rented),
                                                owner_name_db or None,
                                                owner_wa_db or None,
                                                tenant_name_db or None,
                                                tenant_wa_db or None,
                                                int(has_private),
                                                notes_db,
                                                int(is_active),
                                            ),
                                        )
                                        created += 1
                                    except sqlite3.IntegrityError:
                                        skipped += 1
                                        notes.append(f"Fila {i+1}: T{tower_num}-{apt_num} ya existe → omitida (modo solo-crear)")

                            except Exception as e:
                                errors += 1
                                notes.append(f"Fila {i+1}: error → {e}")

                        con.commit()
                        audit(
                            "ADMIN_BULK_IMPORT_APTS",
                            current_user().id,
                            {"created": created, "updated": updated, "skipped": skipped, "errors": errors},
                        )

                        # ✅ Guardar resultado para que no se pierda con st.rerun
                        st.session_state["_ASTR_apt_csv_last_result"] = {
                            "created": created,
                            "updated": updated,
                            "skipped": skipped,
                            "errors": errors,
                            "notes": notes,
                        }

                        st.success(
                            f"Listo ✅ · creados: {created} · actualizados: {updated} · omitidos: {skipped} · errores: {errors}"
                        )
                        st.rerun()

        st.markdown("---")
        st.markdown("### Editar apartamento")

        # Cargar apartamentos (para editar / revisar)
        apts_all = con.execute(
            """
            SELECT a.*,
                   t.tower_num, t.etapa_residencial,
                   t.is_active as tower_active
            FROM apartments a
            JOIN towers t ON t.id=a.tower_id
            ORDER BY t.tower_num, a.apt_number
            """
        ).fetchall()

        if not apts_all:
            st.info("Aún no hay apartamentos creados.")
        else:
            # Columnas opcionales (compatibilidad)
            a_has_rented = _table_has_column(con, "apartments", "is_rented")
            a_has_owner_name = _table_has_column(con, "apartments", "owner_name")
            a_has_owner_wa = _table_has_column(con, "apartments", "owner_whatsapp")
            a_has_tenant_name = _table_has_column(con, "apartments", "tenant_name")
            a_has_tenant_wa = _table_has_column(con, "apartments", "tenant_whatsapp")

            active = [r for r in apts_all if int(r["is_active"]) == 1 and int(r["tower_active"]) == 1]
            inactive = [r for r in apts_all if not (int(r["is_active"]) == 1 and int(r["tower_active"]) == 1)]

            missing_contact_wa = []
            for r in active:
                try:
                    if not str(r["whatsapp"] or "").strip():
                        missing_contact_wa.append(r)
                except Exception:
                    pass

            c1, c2, c3 = st.columns(3)
            c1.metric("Aptos activos", len(active))
            c2.metric("Aptos inactivos", len(inactive))
            c3.metric("Sin WhatsApp (contacto)", len(missing_contact_wa))

            with st.expander("Lista rápida (buscar / filtrar)", expanded=False):
                colf1, colf2, colf3 = st.columns([1.2, 1.2, 2.2])
                with colf1:
                    tower_filter = st.selectbox(
                        "Torre",
                        ["Todas"] + sorted({int(r["tower_num"]) for r in apts_all}),
                        key="ASTR_apt_list_tower",
                    )
                with colf2:
                    only_active = st.checkbox("Solo activos", value=True, key="ASTR_apt_list_only_active")
                with colf3:
                    q = st.text_input(
                        "Buscar (apto / contacto / whatsapp)",
                        placeholder="922, Juan, 300...",
                        key="ASTR_apt_list_q",
                    ).strip().lower()

                rows2 = apts_all
                if tower_filter != "Todas":
                    rows2 = [r for r in rows2 if int(r["tower_num"]) == int(tower_filter)]
                if only_active:
                    rows2 = [r for r in rows2 if int(r["is_active"]) == 1 and int(r["tower_active"]) == 1]
                if q:
                    def _txt(rr):
                        return f"{rr['apt_number']} {rr['resident_name'] or ''} {rr['whatsapp'] or ''}".lower()
                    rows2 = [r for r in rows2 if q in _txt(r)]

                st.caption(f"Mostrando: {len(rows2)} (máx 250)")
                for r in rows2[:250]:
                    status = "✅" if int(r["is_active"]) == 1 and int(r["tower_active"]) == 1 else "⛔"
                    wa_ok = "📲" if str(r["whatsapp"] or "").strip() else "—"
                    rented = ""
                    if a_has_rented:
                        try:
                            rented = " · 🏠 Arrendado" if int(r["is_rented"] or 0) == 1 else ""
                        except Exception:
                            rented = ""
                    priv = " · 🅿️ Privado" if int(r["has_private_parking"]) == 1 else ""
                    name = (r["resident_name"] or "").strip()
                    extra = f" · {name}" if name else ""
                    st.markdown(f"- {status} **T{int(r['tower_num'])}-{r['apt_number']}**{extra} · WA: {wa_ok}{rented}{priv}")

            st.markdown("#### Editar apartamento (detalle)")
            # Para no cargar un selector inmanejable, recortamos a los últimos 1200 registros
            apts_recent = list(apts_all)[-1200:]

            aid = st.selectbox(
                "Apartamento (registro)",
                [int(r["id"]) for r in apts_recent],
                format_func=lambda i: f"{i} · T{next(x['tower_num'] for x in apts_recent if int(x['id'])==i)}-{next(x['apt_number'] for x in apts_recent if int(x['id'])==i)}",
                key="ASTR_apt_edit_sel",
            )
            ar = next(x for x in apts_recent if int(x["id"]) == int(aid))
            k = f"apt_{aid}"

            towers_all = con.execute("SELECT * FROM towers ORDER BY tower_num").fetchall()
            tid_opts = [int(t["id"]) for t in towers_all]
            idx_t = tid_opts.index(int(ar["tower_id"])) if int(ar["tower_id"]) in tid_opts else 0

            new_tower_id = st.selectbox(
                "Torre",
                tid_opts,
                index=idx_t,
                format_func=lambda i: f"Torre {next(x['tower_num'] for x in towers_all if int(x['id'])==i)}" + ("" if int(next(x['is_active'] for x in towers_all if int(x['id'])==i)) == 1 else " (inactiva)"),
                key=f"{k}_tower",
            )
            new_apt = st.text_input("Apartamento", value=str(ar["apt_number"]), key=f"{k}_num")

            # Contacto (quien vive) — usado para operación/portería
            new_contact_name = st.text_input("Nombre (contacto portería)", value=ar["resident_name"] or "", key=f"{k}_res_name")
            new_contact_wa = st.text_input("WhatsApp (contacto portería)", value=(ar["whatsapp"] or ""), key=f"{k}_res_wa")

            # Arrendado y contactos (si existen columnas)
            if a_has_rented:
                cur_rented = 0
                try:
                    cur_rented = int(ar["is_rented"] or 0)
                except Exception:
                    cur_rented = 0
                new_is_rented = st.checkbox("Apartamento arrendado", value=bool(cur_rented), key=f"{k}_rented")
            else:
                new_is_rented = False

            owner_name = (ar["owner_name"] if a_has_owner_name else "") or ""
            owner_wa = (ar["owner_whatsapp"] if a_has_owner_wa else "") or ""
            tenant_name = (ar["tenant_name"] if a_has_tenant_name else "") or ""
            tenant_wa = (ar["tenant_whatsapp"] if a_has_tenant_wa else "") or ""

            if a_has_owner_name or a_has_owner_wa:
                st.caption("📌 Si el apto está arrendado, la sanción se enviará al **propietario** (owner_*).")
                cown1, cown2 = st.columns(2)
                with cown1:
                    owner_name = st.text_input("Nombre propietario", value=str(owner_name), key=f"{k}_owner_name")
                with cown2:
                    owner_wa = st.text_input("WhatsApp propietario", value=str(owner_wa), key=f"{k}_owner_wa")

            if a_has_tenant_name or a_has_tenant_wa:
                cten1, cten2 = st.columns(2)
                with cten1:
                    tenant_name = st.text_input("Nombre inquilino", value=str(tenant_name), key=f"{k}_tenant_name")
                with cten2:
                    tenant_wa = st.text_input("WhatsApp inquilino", value=str(tenant_wa), key=f"{k}_tenant_wa")

            # Infra
            new_priv = st.checkbox("Tiene parqueadero privado", value=bool(int(ar["has_private_parking"])), key=f"{k}_priv")
            new_notes = st.text_input("Notas", value=ar["notes"] or "", key=f"{k}_notes")
            new_active = st.checkbox("Activo", value=bool(int(ar["is_active"])), key=f"{k}_active")

            # Normalizar owner/tenant según lógica
            owner_name_db = (str(owner_name).strip() if (a_has_owner_name or a_has_owner_wa) else None)
            owner_wa_db = (str(owner_wa).strip() if (a_has_owner_name or a_has_owner_wa) else None)
            tenant_name_db = (str(tenant_name).strip() if (a_has_tenant_name or a_has_tenant_wa) else None)
            tenant_wa_db = (str(tenant_wa).strip() if (a_has_tenant_name or a_has_tenant_wa) else None)

            if a_has_rented and not new_is_rented:
                # Si NO está arrendado, por defecto el dueño = contacto; tenant vacío
                if (a_has_owner_name or a_has_owner_wa):
                    if not (owner_name_db or ""):
                        owner_name_db = (new_contact_name or "").strip() or None
                    if not (owner_wa_db or ""):
                        owner_wa_db = (new_contact_wa or "").strip() or None
                if (a_has_tenant_name or a_has_tenant_wa):
                    tenant_name_db = None
                    tenant_wa_db = None
            elif a_has_rented and new_is_rented:
                # Si está arrendado, sugerimos que el contacto = inquilino si el contacto está vacío
                if (not (new_contact_name or "").strip()) and (tenant_name_db or "").strip():
                    new_contact_name = tenant_name_db
                if (not (new_contact_wa or "").strip()) and (tenant_wa_db or "").strip():
                    new_contact_wa = tenant_wa_db

            if st.button("Actualizar apartamento", key=f"{k}_save", type="primary"):
                cols = ["tower_id=?", "apt_number=?", "resident_name=?", "whatsapp=?", "has_private_parking=?", "notes=?", "is_active=?"]
                vals = [int(new_tower_id), new_apt.strip(), new_contact_name.strip() or None, new_contact_wa.strip() or None, 1 if new_priv else 0, new_notes.strip() or None, 1 if new_active else 0]

                if a_has_rented:
                    cols.insert(4, "is_rented=?")
                    vals.insert(4, 1 if new_is_rented else 0)

                if a_has_owner_name:
                    cols.append("owner_name=?")
                    vals.append(owner_name_db or None)
                if a_has_owner_wa:
                    cols.append("owner_whatsapp=?")
                    vals.append(owner_wa_db or None)
                if a_has_tenant_name:
                    cols.append("tenant_name=?")
                    vals.append(tenant_name_db or None)
                if a_has_tenant_wa:
                    cols.append("tenant_whatsapp=?")
                    vals.append(tenant_wa_db or None)

                sql = "UPDATE apartments SET " + ", ".join(cols) + " WHERE id=?"
                vals.append(int(aid))

                try:
                    con.execute(sql, tuple(vals))
                    con.commit()
                    audit("ADMIN_UPDATE_APT", current_user().id, {"id": int(aid), "tower_id": int(new_tower_id), "apt": new_apt.strip(), "is_active": new_active})
                    st.success("Actualizado ✅")
                    st.rerun()
                except sqlite3.IntegrityError:
                    st.error("Ya existe ese apartamento en esa torre (duplicado).")

    # -------------------------
    # ZONAS
    # -------------------------
    with tab3:
        st.markdown("### Crear zona pública ")

        with st.form("cz_create"):
            name = st.text_input("Nombre zona", placeholder="Zona Visitantes Etapa 2 - Carros", key="ASTR_zone_create_name")
            vtype = st.selectbox("Tipo", ["CAR", "MOTO"], format_func=lambda x: "Carros" if x == "CAR" else "Motos", key="ASTR_zone_create_type")
            cap = st.number_input("Cupos", 0, 5000, 0, key="ASTR_zone_create_cap")
            allow_vis = st.checkbox("Permite visitantes", value=True, key="ASTR_zone_create_av")
            allow_rnp = st.checkbox("Permite residentes sin privado", value=True, key="ASTR_zone_create_ar")

            # Etapa de zona (opcional)
            inferred = _extract_etapa_number(name) or 0
            etapa_choice = st.selectbox(
                "Etapa de la zona (opcional)",
                ["No definida"] + list(range(1, 21)),
                index=(1 + inferred - 1) if inferred in range(1, 21) else 0,
                key="ASTR_zone_create_etapa",
            )

            l1 = st.number_input("Umbral Nivel 2", 0.01, 0.99, 0.70, 0.01, key="ASTR_zone_create_l1")
            l2 = st.number_input("Umbral Nivel 3", 0.01, 0.99, 0.85, 0.01, key="ASTR_zone_create_l2")
            l3 = st.number_input("Umbral Nivel 4", 0.01, 0.99, 0.95, 0.01, key="ASTR_zone_create_l3")
            ok = st.form_submit_button("Crear zona")

        if ok:
            if not name.strip():
                st.error("Nombre de zona es obligatorio.")
            else:
                exists = con.execute("SELECT id FROM zones WHERE name=?", (name.strip(),)).fetchone()
                if exists:
                    st.warning("Esa zona ya existe. Edita abajo.")
                else:
                    th = {"l1": float(l1), "l2": float(l2), "l3": float(l3)}
                    etapa_zone_val = None
                    if etapa_choice != "No definida":
                        etapa_zone_val = int(etapa_choice)

                    if zones_has_etapa:
                        con.execute(
                            """
                            INSERT INTO zones(name,vehicle_type,capacity,is_public,allow_visitors,allow_residents_without_private,thresholds_json,etapa_zone,is_active)
                            VALUES (?,?,?,?,?,?,?,?,1)
                            """,
                            (name.strip(), vtype, int(cap), 1, 1 if allow_vis else 0, 1 if allow_rnp else 0, json.dumps(th, ensure_ascii=False), etapa_zone_val),
                        )
                    else:
                        con.execute(
                            """
                            INSERT INTO zones(name,vehicle_type,capacity,is_public,allow_visitors,allow_residents_without_private,thresholds_json,is_active)
                            VALUES (?,?,?,?,?,?,?,1)
                            """,
                            (name.strip(), vtype, int(cap), 1, 1 if allow_vis else 0, 1 if allow_rnp else 0, json.dumps(th, ensure_ascii=False)),
                        )

                    con.commit()
                    audit("ADMIN_CREATE_ZONE", current_user().id, {"name": name.strip(), "vehicle_type": vtype, "capacity": int(cap), "etapa_zone": etapa_zone_val})
                    st.success("Zona creada ✅")
                    st.rerun()

        # Cargar zonas
        if zones_has_etapa:
            zones_all = con.execute(
                """
                SELECT id, name, vehicle_type, capacity, allow_visitors, allow_residents_without_private, thresholds_json, etapa_zone, is_active
                FROM zones
                ORDER BY vehicle_type, name
                """
            ).fetchall()
        else:
            zones_all = con.execute(
                """
                SELECT id, name, vehicle_type, capacity, allow_visitors, allow_residents_without_private, thresholds_json, is_active
                FROM zones
                ORDER BY vehicle_type, name
                """
            ).fetchall()

        if zones_all:
            active = [z for z in zones_all if int(z["is_active"]) == 1]
            inactive = [z for z in zones_all if int(z["is_active"]) == 0]

            total_cap = sum(int(z["capacity"]) for z in active)
            used_rows = con.execute("SELECT zone_id, COUNT(*) as n FROM tickets WHERE exit_time IS NULL AND zone_id IS NOT NULL GROUP BY zone_id").fetchall()
            used_map = {int(r["zone_id"]): int(r["n"]) for r in used_rows}
            total_used = sum(used_map.get(int(z["id"]), 0) for z in active)

            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Zonas activas", len(active))
            c2.metric("Zonas inactivas", len(inactive))
            c3.metric("Capacidad total (activa)", total_cap)
            c4.metric("Ocupados (según sistema)", total_used)

            st.markdown("#### Zonas por tipo (activas)")
            type_agg = {}
            for z in active:
                vt = z["vehicle_type"]
                type_agg.setdefault(vt, {"Zonas": 0, "Capacidad": 0, "Ocupados": 0})
                type_agg[vt]["Zonas"] += 1
                type_agg[vt]["Capacidad"] += int(z["capacity"])
                type_agg[vt]["Ocupados"] += used_map.get(int(z["id"]), 0)

            df_type = pd.DataFrame(
                [
                    {
                        "Tipo": ("Carros" if vt == "CAR" else "Motos"),
                        "Zonas": v["Zonas"],
                        "Capacidad": v["Capacidad"],
                        "Ocupados": v["Ocupados"],
                        "Disponibles": max(v["Capacidad"] - v["Ocupados"], 0),
                    }
                    for vt, v in sorted(type_agg.items(), key=lambda x: x[0])
                ]
            )
            st.dataframe(df_type, width="stretch", hide_index=True)

            with st.expander("Lista rápida (buscar / filtrar)", expanded=False):
                colf1, colf2, colf3 = st.columns([1.1, 1.1, 2.2])
                with colf1:
                    type_filter = st.selectbox("Tipo", ["Todos", "CAR", "MOTO"], key="ASTR_zone_list_type")
                with colf2:
                    only_active = st.checkbox("Solo activas", value=True, key="ASTR_zone_list_only_active")
                with colf3:
                    q = st.text_input("Buscar por nombre", placeholder="Etapa 2, Visitantes...", key="ASTR_zone_list_q").strip().lower()

                rows = zones_all
                if type_filter != "Todos":
                    rows = [z for z in rows if z["vehicle_type"] == type_filter]
                if only_active:
                    rows = [z for z in rows if int(z["is_active"]) == 1]
                if q:
                    rows = [z for z in rows if q in (z["name"] or "").lower()]

                st.caption(f"Mostrando: {len(rows)}")
                for z in rows[:250]:
                    status = "✅" if int(z["is_active"]) == 1 else "⛔"
                    tag = "🚗" if z["vehicle_type"] == "CAR" else "🏍️"
                    used = used_map.get(int(z["id"]), 0)
                    capz = int(z["capacity"])
                    avail = max(capz - used, 0)

                    if zones_has_etapa:
                        ez = z["etapa_zone"]
                    else:
                        ez = _extract_etapa_number(z["name"])
                    etapa_txt = f"Etapa {int(ez)}" if ez not in (None, "", 0) else "Etapa ?"

                    st.markdown(f"- {status} {tag} **{z['name']}** · {etapa_txt} · Cupos: {capz} · Ocupados: {used} · Disp: {avail}")
        else:
            st.info("Aún no hay zonas creadas.")

        st.markdown("### Editar zona")
        if zones_all:
            zid = st.selectbox(
                "Zona",
                [int(z["id"]) for z in zones_all],
                format_func=lambda i: next(x["name"] for x in zones_all if int(x["id"]) == i),
                key="ASTR_zone_edit_sel",
            )
            zr = next(x for x in zones_all if int(x["id"]) == zid)
            k = f"zone_{zid}"

            # thresholds
            th = {}
            try:
                th = json.loads(zr["thresholds_json"] or "{}")
            except Exception:
                th = {}
            l1v = float(th.get("l1", 0.70))
            l2v = float(th.get("l2", 0.85))
            l3v = float(th.get("l3", 0.95))

            new_name = st.text_input("Nombre", value=zr["name"], key=f"{k}_name")
            new_vt = st.selectbox("Tipo", ["CAR", "MOTO"], index=0 if zr["vehicle_type"] == "CAR" else 1, key=f"{k}_type")
            new_cap = st.number_input("Cupos", 0, 5000, int(zr["capacity"]), key=f"{k}_cap")
            new_av = st.checkbox("Permite visitantes", value=bool(int(zr["allow_visitors"])), key=f"{k}_av")
            new_ar = st.checkbox("Permite residentes sin privado", value=bool(int(zr["allow_residents_without_private"])), key=f"{k}_ar")

            if zones_has_etapa:
                cur_ez = zr["etapa_zone"]
                cur_ez = int(cur_ez) if cur_ez not in (None, "", 0) else None
                etapa_pick = st.selectbox(
                    "Etapa de la zona (opcional)",
                    ["No definida"] + list(range(1, 21)),
                    index=(1 + cur_ez - 1) if (cur_ez in range(1, 21)) else 0,
                    key=f"{k}_etapa",
                )
            else:
                etapa_pick = "No definida"

            nl1 = st.number_input("Umbral Nivel 2", 0.01, 0.99, l1v, 0.01, key=f"{k}_l1")
            nl2 = st.number_input("Umbral Nivel 3", 0.01, 0.99, l2v, 0.01, key=f"{k}_l2")
            nl3 = st.number_input("Umbral Nivel 4", 0.01, 0.99, l3v, 0.01, key=f"{k}_l3")
            new_active = st.checkbox("Activa", value=bool(int(zr["is_active"])), key=f"{k}_active")

            if st.button("Actualizar zona", key=f"{k}_save"):
                try:
                    th_json = json.dumps({"l1": float(nl1), "l2": float(nl2), "l3": float(nl3)}, ensure_ascii=False)
                    etapa_zone_val = None
                    if etapa_pick != "No definida":
                        etapa_zone_val = int(etapa_pick)

                    if zones_has_etapa:
                        con.execute(
                            """
                            UPDATE zones
                            SET name=?, vehicle_type=?, capacity=?, allow_visitors=?, allow_residents_without_private=?, thresholds_json=?, etapa_zone=?, is_active=?
                            WHERE id=?
                            """,
                            (new_name.strip(), new_vt, int(new_cap), 1 if new_av else 0, 1 if new_ar else 0, th_json, etapa_zone_val, 1 if new_active else 0, int(zid)),
                        )
                    else:
                        con.execute(
                            """
                            UPDATE zones
                            SET name=?, vehicle_type=?, capacity=?, allow_visitors=?, allow_residents_without_private=?, thresholds_json=?, is_active=?
                            WHERE id=?
                            """,
                            (new_name.strip(), new_vt, int(new_cap), 1 if new_av else 0, 1 if new_ar else 0, th_json, 1 if new_active else 0, int(zid)),
                        )

                    con.commit()
                    audit("ADMIN_UPDATE_ZONE", current_user().id, {"id": int(zid), "name": new_name.strip(), "is_active": new_active, "etapa_zone": etapa_zone_val})
                    st.success("Zona actualizada ✅")
                    st.rerun()
                except sqlite3.IntegrityError:
                    st.error("Ya existe otra zona con ese nombre.")

    # -------------------------
    # CELDAS PRIVADAS (integrado)
    # -------------------------
    with tab_cells:
        st.markdown("## 🅿️ Celdas privadas")
        st.caption("Crea celdas privadas y luego edítalas abajo. Marca PMR cuando aplique.")

        # Solo torres activas para crear/editar asignación
        towers_active = con.execute("SELECT id, tower_num, is_active FROM towers WHERE is_active=1 ORDER BY tower_num").fetchall()
        if not towers_active:
            st.warning("Primero crea torres activas.")
        else:
            # -------------------------
            # Crear celda 
            # -------------------------
            st.markdown("### Crear celda ")

            with st.form("APC_pc_create_form", clear_on_submit=True):
                c1, c2 = st.columns([1.1, 1.2])
                with c1:
                    tower_id_create = st.selectbox(
                        "Torre",
                        [int(t["id"]) for t in towers_active],
                        format_func=lambda i: f"Torre {next(x['tower_num'] for x in towers_active if int(x['id'])==i)}",
                        key="APC_pc_create_tower_id",
                    )
                with c2:
                    apt_number_create = st.text_input("Apartamento", placeholder="922", key="APC_pc_create_apt")

                code_create = st.text_input("Código celda", placeholder="T3-P1-045", key="APC_pc_create_code")
                policy_create = st.selectbox("Política", POLICY_PRESETS, key="APC_pc_create_policy")
                is_pmr_create = st.checkbox("Es PMR", value=False, key="APC_pc_create_pmr")
                is_active_create = st.checkbox("Activa", value=True, key="APC_pc_create_active")

                ok_create = st.form_submit_button("Guardar celda")

            if ok_create:
                apt_number_create = (apt_number_create or "").strip()
                code_create = (code_create or "").strip()

                if not apt_number_create:
                    st.error("Apartamento es obligatorio.")
                elif not code_create:
                    st.error("Código celda es obligatorio.")
                else:
                    # Validar apartamento activo
                    apt = con.execute(
                        "SELECT id FROM apartments WHERE tower_id=? AND apt_number=? AND is_active=1",
                        (int(tower_id_create), apt_number_create),
                    ).fetchone()
                    if not apt:
                        st.error("Apartamento no encontrado o inactivo (solo permite torres y apartamentos activos).")
                    else:
                        # Solo crear: si existe el código, no actualiza aquí
                        ex = con.execute("SELECT id FROM private_cells WHERE code=?", (code_create,)).fetchone()
                        if ex:
                            st.warning("Esa celda ya existe. Ve a **Editar celda** abajo para modificarla.")
                        else:
                            con.execute(
                                "INSERT INTO private_cells(code,apt_id,policy,is_pmr,is_active) VALUES (?,?,?,?,?)",
                                (code_create, int(apt["id"]), policy_create, 1 if is_pmr_create else 0, 1 if is_active_create else 0),
                            )
                            # Marcar que el apto tiene privado
                            con.execute("UPDATE apartments SET has_private_parking=1 WHERE id=?", (int(apt["id"]),))
                            con.commit()
                            audit("ADMIN_CREATE_PRIVATE_CELL", current_user().id, {"code": code_create, "tower_id": int(tower_id_create), "apt_number": apt_number_create})
                            st.success("Celda creada ✅")
                            st.rerun()

        st.markdown("---")

        # -------------------------
        # Lista / métricas
        # -------------------------
        cells_all = con.execute(
            """
            SELECT pc.id, pc.code, pc.policy, pc.is_pmr, pc.is_active,
                   a.id as apt_id, a.apt_number, a.is_active as apt_active,
                   t.id as tower_id, t.tower_num, t.etapa_residencial, t.is_active as tower_active
            FROM private_cells pc
            JOIN apartments a ON a.id=pc.apt_id
            JOIN towers t ON t.id=a.tower_id
            ORDER BY t.tower_num, a.apt_number, pc.code
            """
        ).fetchall()
        cells_all = [dict(r) for r in cells_all] if cells_all else []

        if not cells_all:
            st.info("No hay celdas creadas aún.")
        else:
            only_active = st.checkbox("Mostrar solo activas (torre + apto + celda)", value=True, key="APC_pc_only_active")
            rows_view = cells_all
            if only_active:
                rows_view = [r for r in rows_view if int(r.get("tower_active", 0)) == 1 and int(r.get("apt_active", 0)) == 1 and int(r.get("is_active", 0)) == 1]

            total = len(rows_view)
            active_n = sum(1 for r in rows_view if int(r.get("is_active", 0)) == 1)
            pmr_n = sum(1 for r in rows_view if int(r.get("is_pmr", 0)) == 1 and int(r.get("is_active", 0)) == 1)

            c1, c2, c3 = st.columns(3)
            c1.metric("Celdas", total)
            c2.metric("Activas", active_n)
            c3.metric("PMR", pmr_n)

            with st.expander("Lista (tabla)", expanded=False):
                df_cells = pd.DataFrame(
                    [
                        {
                            "ID": int(r["id"]),
                            "Código": r["code"],
                            "Torre": int(r["tower_num"]),
                            "Apto": r["apt_number"],
                            "Etapa": int(r.get("etapa_residencial") or 0),
                            "Política": r["policy"],
                            "PMR": "Sí" if int(r["is_pmr"]) == 1 else "No",
                            "Activa": "Sí" if int(r["is_active"]) == 1 else "No",
                        }
                        for r in rows_view
                    ]
                )
                st_df(df_cells)

        st.markdown("---")

        # -------------------------
        # Editar celda (completo)
        # -------------------------
        st.markdown("### Editar celda")

        if not cells_all:
            st.info("Crea una celda arriba para poder editar.")
        else:
            selectable = cells_all
            if st.session_state.get("APC_pc_only_active", True):
                selectable = [r for r in cells_all if int(r.get("tower_active", 0)) == 1 and int(r.get("apt_active", 0)) == 1 and int(r.get("is_active", 0)) == 1]
                if not selectable:
                    selectable = cells_all

            pick_id = st.selectbox(
                "Celda",
                [int(r["id"]) for r in selectable],
                format_func=lambda i: f"#{i} · {next(x['code'] for x in selectable if int(x['id'])==i)} · T{next(x['tower_num'] for x in selectable if int(x['id'])==i)}-{next(x['apt_number'] for x in selectable if int(x['id'])==i)}",
                key="APC_pc_edit_pick",
            )
            cur = next(x for x in selectable if int(x["id"]) == int(pick_id))

            towers_all = con.execute("SELECT id, tower_num, is_active FROM towers ORDER BY tower_num").fetchall()
            tower_ids = [int(t["id"]) for t in towers_all]
            cur_tower_id = int(cur["tower_id"])
            idx_t = tower_ids.index(cur_tower_id) if cur_tower_id in tower_ids else 0

            with st.form(f"APC_pc_edit_form_{pick_id}", clear_on_submit=False):
                new_code = st.text_input("Código celda", value=str(cur["code"]), key=f"APC_pc_edit_code_{pick_id}")
                new_tower_id = st.selectbox(
                    "Torre",
                    tower_ids,
                    index=idx_t,
                    format_func=lambda i: f"Torre {next(x['tower_num'] for x in towers_all if int(x['id'])==i)}" + ("" if int(next(x['is_active'] for x in towers_all if int(x['id'])==i)) == 1 else " (inactiva)"),
                    key=f"APC_pc_edit_tower_{pick_id}",
                )
                new_apt_number = st.text_input("Apartamento", value=str(cur["apt_number"]), key=f"APC_pc_edit_apt_{pick_id}")

                new_policy = st.selectbox(
                    "Política",
                    POLICY_PRESETS,
                    index=POLICY_PRESETS.index(cur["policy"]) if cur["policy"] in POLICY_PRESETS else 0,
                    key=f"APC_pc_edit_policy_{pick_id}",
                )
                new_pmr = st.checkbox("Es PMR", value=bool(int(cur["is_pmr"])), key=f"APC_pc_edit_pmr_{pick_id}")
                new_active = st.checkbox("Activa", value=bool(int(cur["is_active"])), key=f"APC_pc_edit_active_{pick_id}")

                ok_save = st.form_submit_button("Actualizar celda")

            if ok_save:
                new_code = (new_code or "").strip()
                new_apt_number = (new_apt_number or "").strip()

                if not new_code:
                    st.error("Código celda es obligatorio.")
                elif not new_apt_number:
                    st.error("Apartamento es obligatorio.")
                else:
                    apt = con.execute(
                        "SELECT id FROM apartments WHERE tower_id=? AND apt_number=? AND is_active=1",
                        (int(new_tower_id), new_apt_number),
                    ).fetchone()
                    if not apt:
                        st.error("Apartamento no encontrado o inactivo (solo permite torres y apartamentos activos).")
                    else:
                        ex = con.execute("SELECT id FROM private_cells WHERE code=? AND id<>?", (new_code, int(pick_id))).fetchone()
                        if ex:
                            st.error("Ya existe otra celda con ese código.")
                        else:
                            con.execute(
                                """
                                UPDATE private_cells
                                SET code=?, apt_id=?, policy=?, is_pmr=?, is_active=?
                                WHERE id=?
                                """,
                                (new_code, int(apt["id"]), new_policy, 1 if new_pmr else 0, 1 if new_active else 0, int(pick_id)),
                            )
                            con.execute("UPDATE apartments SET has_private_parking=1 WHERE id=?", (int(apt["id"]),))
                            con.commit()
                            audit("ADMIN_UPDATE_PRIVATE_CELL", current_user().id, {"id": int(pick_id), "code": new_code})
                            st.success("Actualizada ✅")
                            st.rerun()


    # -------------------------
    # VEHÍCULOS RESIDENTES (integrado)
    # -------------------------
    with tab_veh:
        st.markdown("## 🚘 Vehículos residentes")
        limits = get_config(con, "resident_limits", {"max_cars": 1, "max_motos": 2})
        st.caption(f"Límites por apto: {limits.get('max_cars',1)} carro(s) · {limits.get('max_motos',2)} moto(s)")

        towers_active = con.execute("SELECT id, tower_num FROM towers WHERE is_active=1 ORDER BY tower_num").fetchall()
        if not towers_active:
            st.warning("Primero crea torres activas.")
        else:
            # -------------------------
            # Crear vehículo 
            # -------------------------
            st.markdown("### Crear vehículo ")

            with st.form("AVH_veh_create_form", clear_on_submit=True):
                c1, c2 = st.columns([1.1, 1.2])
                with c1:
                    tower_id_create = st.selectbox(
                        "Torre",
                        [int(t["id"]) for t in towers_active],
                        format_func=lambda i: f"Torre {next(x['tower_num'] for x in towers_active if int(x['id'])==i)}",
                        key="AVH_veh_create_tower_id",
                    )
                with c2:
                    apt_number_create = st.text_input("Apartamento", placeholder="922", key="AVH_veh_create_apt")

                plate_in = st.text_input("Placa", placeholder="ABC123", key="AVH_veh_create_plate")
                vtype = st.selectbox("Tipo", ["CAR", "MOTO"], key="AVH_veh_create_type")
                brand = st.text_input("Marca (opcional)", key="AVH_veh_create_brand")
                color = st.text_input("Color (opcional)", key="AVH_veh_create_color")
                pmr = st.checkbox("Autorizado PMR", value=False, key="AVH_veh_create_pmr")
                is_active = st.checkbox("Activo", value=True, key="AVH_veh_create_active")

                ok_create = st.form_submit_button("Guardar vehículo")

            if ok_create:
                apt_number_create = (apt_number_create or "").strip()
                plate_raw = (plate_in or "").strip()

                if not apt_number_create:
                    st.error("Apartamento es obligatorio.")
                else:
                    apt = con.execute(
                        "SELECT id FROM apartments WHERE tower_id=? AND apt_number=? AND is_active=1",
                        (int(tower_id_create), apt_number_create),
                    ).fetchone()
                    if not apt:
                        st.error("Apartamento no encontrado o inactivo (solo permite torres y apartamentos activos).")
                    else:
                        plate = normalize_plate(plate_raw)
                        if not plate_is_valid(plate):
                            st.error("Placa inválida.")
                        else:
                            ex = con.execute("SELECT id FROM resident_vehicles WHERE plate=?", (plate,)).fetchone()
                            if ex:
                                st.warning("Esa placa ya existe. Ve a **Editar vehículo** abajo para modificarla.")
                            else:
                                con.execute(
                                    """
                                    INSERT INTO resident_vehicles(plate,vehicle_type,apt_id,brand,color,is_pmr_authorized,is_active,created_at)
                                    VALUES (?,?,?,?,?,?,?,?)
                                    """,
                                    (plate, vtype, int(apt["id"]), brand.strip() or None, color.strip() or None, 1 if pmr else 0, 1 if is_active else 0, now_tz().isoformat()),
                                )
                                con.commit()
                                audit("ADMIN_CREATE_RESIDENT_VEHICLE", current_user().id, {"plate": plate, "tower_id": int(tower_id_create), "apt_number": apt_number_create, "vehicle_type": vtype})
                                st.success("Vehículo creado ✅")
                                st.rerun()

        st.markdown("---")

        # -------------------------
        # Carga masiva (CSV) — Vehículos
        # -------------------------
        with st.expander("📥 Carga masiva (CSV) — Vehículos", expanded=False):
            st.caption("Sube un CSV para crear o actualizar vehículos residentes. Tip: primero carga torres y apartamentos.")

            # Mostrar resultado anterior (no se pierde con rerun)
            last = st.session_state.get("ASTR_rv_last_import")
            if last:
                st.info(
                    f"Última carga: creados={last.get('created',0)} · actualizados={last.get('updated',0)} · "
                    f"omitidos={last.get('skipped',0)} · errores={last.get('errors',0)}"
                )
                if last.get("error_rows"):
                    with st.expander("Ver errores (última carga)", expanded=False):
                        st_df(pd.DataFrame(last["error_rows"]))

            sample_rv = pd.DataFrame(
                [
                    {
                        "plate": "ABC123",
                        "vehicle_type": "CAR",
                        "tower_num": 3,
                        "apt_number": "922",
                        "brand": "",
                        "color": "",
                        "is_pmr_authorized": 0,
                        "is_active": 1,
                    }
                ]
            )
            st.download_button(
                "⬇️ Descargar plantilla CSV (vehículos)",
                data=sample_rv.to_csv(index=False, sep=";").encode("utf-8-sig"),
                file_name="plantilla_vehiculos.csv",
                mime="text/csv",
                key="ASTR_rv_csv_tpl",
            )

            upsert = st.checkbox("Si existe, actualizar", value=True, key="ASTR_rv_csv_upsert")
            f = st.file_uploader("Subir CSV de vehículos", type=["csv"], key="ASTR_rv_csv_file")
            if f is not None:
                try:
                    raw = f.getvalue()

                    text = None
                    for enc in ("utf-8-sig", "utf-8", "latin1"):
                        try:
                            text = raw.decode(enc)
                            break
                        except Exception:
                            pass

                    if text is None:
                        st.error("No pude leer el archivo. Exporta como CSV UTF-8.")
                        st.stop()

                    df_in = pd.read_csv(
                        io.StringIO(text),
                        sep=None,          # auto-detecta , o ;
                        engine="python",
                        dtype=str,
                        keep_default_na=False,
                    )
                    df_in.columns = [c.strip() for c in df_in.columns]
                except Exception as e:
                    st.error("No pude leer el CSV.")
                    st.exception(e)
                    df_in = None

                if df_in is not None:
                    st.dataframe(df_in.head(30), width="stretch", hide_index=True)

                    def _to_bool(x, default=0) -> int:
                        if x is None or (isinstance(x, float) and pd.isna(x)):
                            return int(default)
                        s = str(x).strip().lower()
                        if s in ("1", "true", "t", "si", "sí", "yes", "y"):
                            return 1
                        if s in ("0", "false", "f", "no", "n"):
                            return 0
                        return int(default)

                    if st.button("Procesar CSV (vehículos)", key="ASTR_rv_csv_run", type="primary"):
                        apts_rows = con.execute(
                            """
                            SELECT a.id, a.apt_number, t.tower_num
                            FROM apartments a JOIN towers t ON t.id=a.tower_id
                            WHERE a.is_active=1 AND t.is_active=1
                            """
                        ).fetchall()
                        apts_map = {(int(r["tower_num"]), str(r["apt_number"]).strip()): int(r["id"]) for r in apts_rows}

                        created = 0
                        updated = 0
                        skipped = 0
                        errors = 0
                        error_rows = []

                        for idx, r in df_in.iterrows():
                            try:
                                plate_raw = str(r.get("plate") or "").strip()
                                if not plate_raw:
                                    skipped += 1
                                    continue
                                plate = normalize_plate(plate_raw)
                                if not plate_is_valid(plate):
                                    skipped += 1
                                    error_rows.append({"fila": int(idx) + 2, "error": "Placa inválida", "plate": plate_raw})
                                    continue

                                tower_num = int(str(r.get("tower_num") or "").strip() or "0")
                                apt_num = str(r.get("apt_number") or "").strip()
                                apt_id = apts_map.get((tower_num, apt_num))
                                if not apt_id:
                                    skipped += 1
                                    error_rows.append({"fila": int(idx) + 2, "error": "Apto no encontrado/activo", "tower_num": tower_num, "apt_number": apt_num})
                                    continue

                                vtype = str(r.get("vehicle_type") or "CAR").strip().upper()
                                if vtype not in ("CAR", "MOTO"):
                                    vtype = "CAR"

                                brand = str(r.get("brand") or "").strip() or None
                                color = str(r.get("color") or "").strip() or None
                                pmr = _to_bool(r.get("is_pmr_authorized", 0), 0)
                                is_active = _to_bool(r.get("is_active", 1), 1)

                                if upsert:
                                    try:
                                        con.execute(
                                            """
                                            INSERT INTO resident_vehicles(plate,vehicle_type,apt_id,brand,color,is_pmr_authorized,is_active,created_at)
                                            VALUES (?,?,?,?,?,?,?,?)
                                            """,
                                            (plate, vtype, int(apt_id), brand, color, pmr, is_active, now_tz().isoformat()),
                                        )
                                        created += 1
                                    except sqlite3.IntegrityError:
                                        con.execute(
                                            """
                                            UPDATE resident_vehicles
                                            SET vehicle_type=?, apt_id=?, brand=?, color=?, is_pmr_authorized=?, is_active=?
                                            WHERE plate=?
                                            """,
                                            (vtype, int(apt_id), brand, color, pmr, is_active, plate),
                                        )
                                        updated += 1
                                else:
                                    try:
                                        con.execute(
                                            """
                                            INSERT INTO resident_vehicles(plate,vehicle_type,apt_id,brand,color,is_pmr_authorized,is_active,created_at)
                                            VALUES (?,?,?,?,?,?,?,?)
                                            """,
                                            (plate, vtype, int(apt_id), brand, color, pmr, is_active, now_tz().isoformat()),
                                        )
                                        created += 1
                                    except sqlite3.IntegrityError:
                                        skipped += 1
                            except Exception as e:
                                errors += 1
                                error_rows.append({"fila": int(idx) + 2, "error": str(e)[:120]})
                                continue

                        con.commit()
                        audit("ADMIN_BULK_IMPORT_RV", current_user().id, {"created": created, "updated": updated, "skipped": skipped, "errors": errors})
                        st.session_state["ASTR_rv_last_import"] = {
                            "created": created,
                            "updated": updated,
                            "skipped": skipped,
                            "errors": errors,
                            "error_rows": error_rows[:50],
                        }
                        st.success(f"Listo ✅ · creados: {created} · actualizados: {updated} · omitidos: {skipped} · errores: {errors}")
                        st.rerun()

        st.markdown("---")

        # -------------------------
        # Lista / métricas
        # -------------------------
        veh_all = con.execute(
            """
            SELECT rv.id, rv.plate, rv.vehicle_type, rv.brand, rv.color, rv.is_pmr_authorized, rv.is_active, rv.created_at,
                   a.id as apt_id, a.apt_number, a.is_active as apt_active,
                   t.id as tower_id, t.tower_num, t.etapa_residencial, t.is_active as tower_active
            FROM resident_vehicles rv
            JOIN apartments a ON a.id=rv.apt_id
            JOIN towers t ON t.id=a.tower_id
            ORDER BY rv.created_at DESC
            """
        ).fetchall()
        veh_all = [dict(r) for r in veh_all] if veh_all else []

        if not veh_all:
            st.info("No hay vehículos residentes registrados.")
        else:
            only_active = st.checkbox("Mostrar solo activos (torre + apto + vehículo)", value=True, key="AVH_veh_only_active")
            rows_view = veh_all
            if only_active:
                rows_view = [r for r in rows_view if int(r.get("tower_active", 0)) == 1 and int(r.get("apt_active", 0)) == 1 and int(r.get("is_active", 0)) == 1]

            total = len(rows_view)
            active_n = sum(1 for r in rows_view if int(r.get("is_active", 0)) == 1)
            cars_n = sum(1 for r in rows_view if (r.get("vehicle_type") == "CAR") and int(r.get("is_active", 0)) == 1)
            motos_n = sum(1 for r in rows_view if (r.get("vehicle_type") == "MOTO") and int(r.get("is_active", 0)) == 1)
            pmr_n = sum(1 for r in rows_view if int(r.get("is_pmr_authorized", 0)) == 1 and int(r.get("is_active", 0)) == 1)

            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Vehículos", total)
            c2.metric("Activos", active_n)
            c3.metric("🚗 Carros", cars_n)
            c4.metric("🏍️ Motos", motos_n)
            st.caption(f"PMR activos: **{pmr_n}**")

            with st.expander("Lista (tabla)", expanded=False):
                dfv = pd.DataFrame(
                    [
                        {
                            "ID": int(r["id"]),
                            "Placa": r["plate"],
                            "Tipo": r["vehicle_type"],
                            "Torre": int(r["tower_num"]),
                            "Apto": r["apt_number"],
                            "Etapa": int(r.get("etapa_residencial") or 0),
                            "Marca": r.get("brand") or "",
                            "Color": r.get("color") or "",
                            "PMR": "Sí" if int(r.get("is_pmr_authorized", 0)) == 1 else "No",
                            "Activo": "Sí" if int(r.get("is_active", 0)) == 1 else "No",
                        }
                        for r in rows_view
                    ]
                )
                st_df(dfv)

        st.markdown("---")

        # -------------------------
        # Editar vehículo (completo)
        # -------------------------
        st.markdown("### Editar vehículo")

        if not veh_all:
            st.info("Crea un vehículo arriba para poder editar.")
        else:
            selectable = veh_all
            if st.session_state.get("AVH_veh_only_active", True):
                selectable = [r for r in veh_all if int(r.get("tower_active", 0)) == 1 and int(r.get("apt_active", 0)) == 1 and int(r.get("is_active", 0)) == 1]
                if not selectable:
                    selectable = veh_all

            pick_id = st.selectbox(
                "Vehículo",
                [int(r["id"]) for r in selectable],
                format_func=lambda i: f"#{i} · {next(x['plate'] for x in selectable if int(x['id'])==i)} · T{next(x['tower_num'] for x in selectable if int(x['id'])==i)}-{next(x['apt_number'] for x in selectable if int(x['id'])==i)}",
                key="AVH_veh_edit_pick",
            )
            cur = next(x for x in selectable if int(x["id"]) == int(pick_id))

            towers_all = con.execute("SELECT id, tower_num, is_active FROM towers ORDER BY tower_num").fetchall()
            tower_ids = [int(t["id"]) for t in towers_all]
            cur_tower_id = int(cur["tower_id"])
            idx_t = tower_ids.index(cur_tower_id) if cur_tower_id in tower_ids else 0

            st.caption("Nota: si cambias la placa, se actualiza solo en **vehículos residentes** (no cambia historiales anteriores).")

            with st.form(f"AVH_veh_edit_form_{pick_id}", clear_on_submit=False):
                new_plate_in = st.text_input("Placa", value=str(cur["plate"]), key=f"AVH_veh_edit_plate_{pick_id}")
                new_vtype = st.selectbox("Tipo", ["CAR", "MOTO"], index=0 if cur["vehicle_type"] == "CAR" else 1, key=f"AVH_veh_edit_type_{pick_id}")

                c1, c2 = st.columns([1.1, 1.2])
                with c1:
                    new_tower_id = st.selectbox(
                        "Torre",
                        tower_ids,
                        index=idx_t,
                        format_func=lambda i: f"Torre {next(x['tower_num'] for x in towers_all if int(x['id'])==i)}" + ("" if int(next(x['is_active'] for x in towers_all if int(x['id'])==i)) == 1 else " (inactiva)"),
                        key=f"AVH_veh_edit_tower_{pick_id}",
                    )
                with c2:
                    new_apt_number = st.text_input("Apartamento", value=str(cur["apt_number"]), key=f"AVH_veh_edit_apt_{pick_id}")

                new_brand = st.text_input("Marca (opcional)", value=str(cur.get("brand") or ""), key=f"AVH_veh_edit_brand_{pick_id}")
                new_color = st.text_input("Color (opcional)", value=str(cur.get("color") or ""), key=f"AVH_veh_edit_color_{pick_id}")
                new_pmr = st.checkbox("Autorizado PMR", value=bool(int(cur.get("is_pmr_authorized", 0))), key=f"AVH_veh_edit_pmr_{pick_id}")
                new_active = st.checkbox("Activo", value=bool(int(cur.get("is_active", 0))), key=f"AVH_veh_edit_active_{pick_id}")

                ok_save = st.form_submit_button("Actualizar vehículo")

            if ok_save:
                new_apt_number = (new_apt_number or "").strip()
                if not new_apt_number:
                    st.error("Apartamento es obligatorio.")
                else:
                    apt = con.execute(
                        "SELECT id FROM apartments WHERE tower_id=? AND apt_number=? AND is_active=1",
                        (int(new_tower_id), new_apt_number),
                    ).fetchone()
                    if not apt:
                        st.error("Apartamento no encontrado o inactivo (solo permite torres y apartamentos activos).")
                    else:
                        new_plate = normalize_plate((new_plate_in or "").strip())
                        if not plate_is_valid(new_plate):
                            st.error("Placa inválida.")
                        else:
                            if new_plate != cur["plate"]:
                                ex = con.execute("SELECT id FROM resident_vehicles WHERE plate=? AND id<>?", (new_plate, int(pick_id))).fetchone()
                                if ex:
                                    st.error("Ya existe otro vehículo con esa placa.")
                                    con.rollback()
                                    return

                            try:
                                con.execute(
                                    """
                                    UPDATE resident_vehicles
                                    SET plate=?, vehicle_type=?, apt_id=?, brand=?, color=?, is_pmr_authorized=?, is_active=?
                                    WHERE id=?
                                    """,
                                    (
                                        new_plate,
                                        new_vtype,
                                        int(apt["id"]),
                                        new_brand.strip() or None,
                                        new_color.strip() or None,
                                        1 if new_pmr else 0,
                                        1 if new_active else 0,
                                        int(pick_id),
                                    ),
                                )
                                con.commit()
                                audit("ADMIN_UPDATE_RESIDENT_VEHICLE", current_user().id, {"id": int(pick_id), "plate": new_plate})
                                st.success("Actualizado ✅")
                                st.rerun()
                            except sqlite3.IntegrityError:
                                st.error("No se pudo actualizar (placa duplicada u otra restricción).")

# ZONAS ↔ TORRES (CERCANÍA)
    # -------------------------
    with tab4:
        st.markdown("### Configurar cercanía (por torre)")
        st.caption("Regla recomendada: misma etapa primero, luego etapas vecinas. CAR y MOTO se ordenan por separado.")

        towers = con.execute("SELECT id, tower_num, etapa_residencial, is_active FROM towers ORDER BY tower_num").fetchall()
        zones_active = con.execute(
            "SELECT * FROM zones WHERE is_active=1 AND is_public=1 ORDER BY vehicle_type, name"
        ).fetchall()

        if not towers or not zones_active:
            st.info("Necesitas torres y zonas públicas activas.")
        else:
            # Torre a configurar
            tid = st.selectbox(
                "Torre",
                [int(t["id"]) for t in towers],
                format_func=lambda i: f"Torre {next(x['tower_num'] for x in towers if int(x['id'])==i)}",
                key="ASTR_link_tower_sel",
            )
            trow = next(x for x in towers if int(x["id"]) == tid)
            tower_num = int(trow["tower_num"])
            tower_etapa = int(trow["etapa_residencial"])
            st.info(f"📌 Torre seleccionada: **T{tower_num}** · Etapa **{tower_etapa}**")

            # Mapa de ocupación para mostrar info útil
            used_rows = con.execute(
                "SELECT zone_id, COUNT(*) as n FROM tickets WHERE exit_time IS NULL AND zone_id IS NOT NULL GROUP BY zone_id"
            ).fetchall()
            used_map = {int(r["zone_id"]): int(r["n"]) for r in used_rows}

            def _zone_etapa(z) -> int | None:
                if zones_has_etapa and ("etapa_zone" in z.keys()) and z["etapa_zone"] is not None:
                    try:
                        return int(z["etapa_zone"])
                    except Exception:
                        return None
                return _extract_etapa_number(z["name"])

            def _zone_label(zid: int) -> str:
                z = next(x for x in zones_active if int(x["id"]) == int(zid))
                ez = _zone_etapa(z)
                etapa_txt = f"Etapa {ez}" if ez else "Etapa ?"
                vt = "Carros" if z["vehicle_type"] == "CAR" else "Motos"
                return f"{vt} · {etapa_txt} · {z['name']}"

            # -------------------------
            # Crear asociación (SOLO CREAR)
            # -------------------------
            st.markdown("#### Crear asociación ")
            colA, colB = st.columns([3, 1])
            with colA:
                zid = st.selectbox(
                    "Zona",
                    [int(z["id"]) for z in zones_active],
                    format_func=_zone_label,
                    key="ASTR_link_create_zone",
                )
            with colB:
                pr = st.number_input("Prioridad", 1, 200, 1, key="ASTR_link_create_pr")

            if st.button("Guardar asociación", key="ASTR_link_create_save", type="primary"):
                try:
                    con.execute(
                        "INSERT INTO zone_tower(zone_id,tower_id,priority,is_active) VALUES (?,?,?,1)",
                        (int(zid), int(tid), int(pr)),
                    )
                    con.commit()
                    audit("ADMIN_CREATE_ZONE_TOWER", current_user().id, {"zone_id": int(zid), "tower_id": int(tid), "priority": int(pr)})
                    st.success("Asociación creada ✅")
                    st.rerun()
                except sqlite3.IntegrityError:
                    st.warning("Esa asociación ya existe. **Edítala abajo** en “Editar asociación” (no se actualiza aquí).")

            # -------------------------
            # Auto-generación por etapa (CAR separado de MOTO)
            # -------------------------
            with st.expander("⚡ Generar cercanía automáticamente (por etapa)", expanded=False):
                mode = st.radio("Modo", ["Reemplazar prioridades (recomendado)", "Solo completar faltantes"], index=0, key="ASTR_link_auto_mode")
                if st.button("Generar", key="ASTR_link_auto_btn"):
                    replace_all = (mode == "Reemplazar prioridades (recomendado)")

                    zones2 = con.execute("SELECT * FROM zones WHERE is_active=1 AND is_public=1 ORDER BY vehicle_type, name").fetchall()

                    created = 0
                    updated = 0
                    skipped = 0

                    for vtype in ["CAR", "MOTO"]:
                        cand = [z for z in zones2 if z["vehicle_type"] == vtype]

                        scored = []
                        for z in cand:
                            ez = _zone_etapa(z)
                            if ez is None:
                                dist = 999
                                side = 1
                                ezn = 999
                            else:
                                ezn = int(ez)
                                dist = abs(ezn - int(tower_etapa))
                                # ✅ Empate: preferir etapas menores primero (ej etapa1 antes que etapa3)
                                side = 0 if ezn <= int(tower_etapa) else 1

                            scored.append((dist, side, ezn, z["name"], int(z["id"])))

                        scored.sort(key=lambda x: (x[0], x[1], x[2], x[3]))

                        prio = 1
                        for _, _, _, _, zone_id in scored:
                            if not replace_all:
                                ex = con.execute(
                                    "SELECT id FROM zone_tower WHERE tower_id=? AND zone_id=?",
                                    (int(tid), int(zone_id)),
                                ).fetchone()
                                if ex:
                                    skipped += 1
                                    continue

                            try:
                                con.execute(
                                    "INSERT INTO zone_tower(zone_id,tower_id,priority,is_active) VALUES (?,?,?,1)",
                                    (int(zone_id), int(tid), int(prio)),
                                )
                                created += 1
                            except sqlite3.IntegrityError:
                                con.execute(
                                    "UPDATE zone_tower SET priority=?, is_active=1 WHERE zone_id=? AND tower_id=?",
                                    (int(prio), int(zone_id), int(tid)),
                                )
                                updated += 1
                            prio += 1

                    con.commit()
                    audit("ADMIN_AUTO_ZONE_TOWER", current_user().id, {"tower_id": int(tid), "created": created, "updated": updated, "skipped": skipped, "replace_all": replace_all})
                    st.success(f"Listo ✅ · creadas: {created} · actualizadas: {updated} · omitidas: {skipped}")
                    st.rerun()

            # -------------------------
            # Asociaciones actuales (resumen + lista organizada)
            # -------------------------
            links_tower = con.execute(
                """
                SELECT zt.id, zt.priority, zt.is_active,
                       z.id as zone_id, z.name as zone_name, z.vehicle_type as vehicle_type,
                       z.capacity as capacity, z.allow_visitors as allow_visitors, z.allow_residents_without_private as allow_rnp,
                       z.is_active as zone_active
                FROM zone_tower zt
                JOIN zones z ON z.id=zt.zone_id
                WHERE zt.tower_id=?
                ORDER BY z.vehicle_type, zt.priority, z.name
                """,
                (tid,),
            ).fetchall()

            st.markdown("#### Resumen (torre seleccionada)")
            if not links_tower:
                st.info("Aún no hay asociaciones para esta torre.")
            else:
                # alertas básicas
                def _active_link(l):
                    return int(l["is_active"]) == 1 and int(l["zone_active"]) == 1

                active_links = [l for l in links_tower if _active_link(l)]
                car_links = [l for l in active_links if l["vehicle_type"] == "CAR"]
                moto_links = [l for l in active_links if l["vehicle_type"] == "MOTO"]

                c1, c2, c3, c4 = st.columns(4)
                c1.metric("Asoc. activas", len(active_links))
                c2.metric("🚗 Carros", len(car_links))
                c3.metric("🏍️ Motos", len(moto_links))
                c4.metric("Total asoc.", len(links_tower))

                # prioridades duplicadas por tipo
                dup_warn = []
                for vt, lst in [("CAR", car_links), ("MOTO", moto_links)]:
                    prios = [int(x["priority"]) for x in lst]
                    dups = sorted({p for p in prios if prios.count(p) > 1})
                    if dups:
                        dup_warn.append(f"{'Carros' if vt=='CAR' else 'Motos'}: prioridades duplicadas {dups}")

                # faltantes por tipo
                if not car_links:
                    dup_warn.append("Falta al menos una zona activa de **Carros** para esta torre.")
                if not moto_links:
                    dup_warn.append("Falta al menos una zona activa de **Motos** para esta torre.")

                if dup_warn:
                    st.warning("⚠️ Alertas:\n- " + "\n- ".join(dup_warn))

                # Lista por tipo
                def _line(l):
                    used = used_map.get(int(l["zone_id"]), 0)
                    cap = int(l["capacity"] or 0)
                    avail = max(cap - used, 0)
                    status = "✅" if _active_link(l) else "⛔"
                    vt_icon = "🚗" if l["vehicle_type"] == "CAR" else "🏍️"
                    flags = []
                    if int(l["allow_visitors"]) == 1:
                        flags.append("VIS")
                    if int(l["allow_rnp"]) == 1:
                        flags.append("RNP")
                    flags_txt = f" ({','.join(flags)})" if flags else ""
                    return f"- {status} P{int(l['priority'])} {vt_icon} **{l['zone_name']}**{flags_txt} · Cupos {cap} · Ocup {used} · Disp {avail}"

                st.markdown("#### Carros (orden por prioridad)")
                if car_links:
                    st.markdown("\n".join([_line(l) for l in car_links]))
                else:
                    st.caption("Sin asociaciones activas de Carros.")

                st.markdown("#### Motos (orden por prioridad)")
                if moto_links:
                    st.markdown("\n".join([_line(l) for l in moto_links]))
                else:
                    st.caption("Sin asociaciones activas de Motos.")

            # -------------------------
            # Editar asociación (AQUÍ sí se edita)
            # -------------------------
            st.markdown("### Editar asociación")
            if links_tower:
                lid = st.selectbox(
                    "Asociación",
                    [int(r["id"]) for r in links_tower],
                    format_func=lambda i: f"#{i} · {('Carros' if next(x['vehicle_type'] for x in links_tower if int(x['id'])==i)=='CAR' else 'Motos')} · "
                                          f"P{next(x['priority'] for x in links_tower if int(x['id'])==i)} · "
                                          f"{next(x['zone_name'] for x in links_tower if int(x['id'])==i)}",
                    key="ASTR_link_edit_sel",
                )
                lr = next(x for x in links_tower if int(x["id"]) == lid)
                new_pr = st.number_input("Prioridad", 1, 200, int(lr["priority"]), key=f"ASTR_link_pr_{lid}")
                new_active = st.checkbox("Activa", value=bool(int(lr["is_active"])), key=f"ASTR_link_act_{lid}")

                if st.button("Actualizar asociación", key=f"ASTR_link_save_{lid}", type="primary"):
                    con.execute("UPDATE zone_tower SET priority=?, is_active=? WHERE id=?", (int(new_pr), 1 if new_active else 0, int(lid)))
                    con.commit()
                    audit("ADMIN_UPDATE_ZONE_TOWER", current_user().id, {"id": int(lid), "priority": int(new_pr), "is_active": new_active})
                    st.success("Actualizada ✅")
                    st.rerun()

    con.close()


# -----------------------------
# ADMIN: Celdas privadas
# -----------------------------
def admin_private_cells():
    require_role("ADMIN")
    con = db_connect()
    st.subheader("🅿️ Celdas privadas")

    towers = con.execute("SELECT * FROM towers WHERE is_active=1 ORDER BY tower_num").fetchall()
    if not towers:
        st.info("Primero crea torres.")
        con.close()
        return
    tnums = [int(t["tower_num"]) for t in towers]

    with st.expander("Crear / actualizar celda", expanded=False):
        tower_num = st.selectbox("Torre", tnums, key="APC_pc_create_tower")
        apt_number = st.text_input("Apartamento", placeholder="922", key="APC_pc_create_apt")
        code = st.text_input("Código celda", placeholder="T3-P1-045", key="APC_pc_create_code")
        policy = st.selectbox("Política", POLICY_PRESETS, key="APC_pc_create_policy")
        is_pmr = st.checkbox("Es PMR", value=False, key="APC_pc_create_pmr")
        if st.button("Guardar celda", key="APC_pc_create_save"):
            apt = con.execute(
                """
                SELECT a.* FROM apartments a JOIN towers t ON t.id=a.tower_id
                WHERE t.tower_num=? AND a.apt_number=? AND a.is_active=1 AND t.is_active=1
                """,
                (int(tower_num), apt_number.strip()),
            ).fetchone()
            if not apt:
                st.error("Apartamento no encontrado o inactivo.")
            elif not code.strip():
                st.error("Código obligatorio.")
            else:
                try:
                    con.execute("INSERT INTO private_cells(code,apt_id,policy,is_pmr,is_active) VALUES (?,?,?,?,1)", (code.strip(), int(apt["id"]), policy, 1 if is_pmr else 0))
                    con.commit()
                except sqlite3.IntegrityError:
                    con.execute("UPDATE private_cells SET apt_id=?, policy=?, is_pmr=?, is_active=1 WHERE code=?", (int(apt["id"]), policy, 1 if is_pmr else 0, code.strip()))
                    con.commit()
                con.execute("UPDATE apartments SET has_private_parking=1 WHERE id=?", (int(apt["id"]),))
                con.commit()
                audit("ADMIN_SAVE_PRIVATE_CELL", current_user().id, {"code": code.strip()})
                st.success("Guardada ✅")
                st.rerun()

    cells = con.execute(
        """
        SELECT pc.*, t.tower_num, a.apt_number
        FROM private_cells pc
        JOIN apartments a ON a.id=pc.apt_id
        JOIN towers t ON t.id=a.tower_id
        ORDER BY pc.code
        """
    ).fetchall()
    st.dataframe(pd.DataFrame([dict(r) for r in cells]) if cells else pd.DataFrame(), width="stretch", hide_index=True)
    con.close()


# -----------------------------
# ADMIN: Vehículos
# -----------------------------
def admin_vehicles():
    require_role("ADMIN")
    con = db_connect()
    st.subheader("🚘 Vehículos residentes")

    limits = get_config(con, "resident_limits", {"max_cars": 1, "max_motos": 2})
    st.caption(f"Límites por apto: {limits.get('max_cars',1)} carro(s) · {limits.get('max_motos',2)} moto(s)")

    towers = con.execute("SELECT * FROM towers WHERE is_active=1 ORDER BY tower_num").fetchall()
    if not towers:
        st.info("Primero crea torres.")
        con.close()
        return

    with st.expander("Crear / actualizar vehículo", expanded=False):
        tower_num = st.selectbox("Torre", [int(t["tower_num"]) for t in towers], key="AVH_veh_create_tower")
        apt_number = st.text_input("Apartamento", placeholder="922", key="AVH_veh_create_apt")
        plate_in = st.text_input("Placa", placeholder="ABC123", key="AVH_veh_create_plate")
        vtype = st.selectbox("Tipo", ["CAR", "MOTO"], key="AVH_veh_create_type")
        brand = st.text_input("Marca", key="AVH_veh_create_brand")
        color = st.text_input("Color", key="AVH_veh_create_color")
        pmr = st.checkbox("Autorizado PMR", value=False, key="AVH_veh_create_pmr")
        if st.button("Guardar vehículo", key="AVH_veh_create_save"):
            apt = con.execute(
                """
                SELECT a.* FROM apartments a JOIN towers t ON t.id=a.tower_id
                WHERE t.tower_num=? AND a.apt_number=? AND a.is_active=1 AND t.is_active=1
                """,
                (int(tower_num), apt_number.strip()),
            ).fetchone()
            if not apt:
                st.error("Apartamento no encontrado o inactivo.")
            else:
                plate = normalize_plate(plate_in)
                if not plate_is_valid(plate):
                    st.error("Placa inválida.")
                else:
                    try:
                        con.execute(
                            """
                            INSERT INTO resident_vehicles(plate,vehicle_type,apt_id,brand,color,is_pmr_authorized,is_active,created_at)
                            VALUES (?,?,?,?,?,?,1,?)
                            """,
                            (plate, vtype, int(apt["id"]), brand.strip() or None, color.strip() or None, 1 if pmr else 0, now_tz().isoformat()),
                        )
                        con.commit()
                    except sqlite3.IntegrityError:
                        con.execute(
                            """
                            UPDATE resident_vehicles
                            SET vehicle_type=?, apt_id=?, brand=?, color=?, is_pmr_authorized=?, is_active=1
                            WHERE plate=?
                            """,
                            (vtype, int(apt["id"]), brand.strip() or None, color.strip() or None, 1 if pmr else 0, plate),
                        )
                        con.commit()
                    audit("ADMIN_SAVE_RESIDENT_VEHICLE", current_user().id, {"plate": plate})
                    st.success("Guardado ✅")
                    st.rerun()

    rows = con.execute(
        """
        SELECT rv.*, t.tower_num, a.apt_number
        FROM resident_vehicles rv
        JOIN apartments a ON a.id=rv.apt_id
        JOIN towers t ON t.id=a.tower_id
        ORDER BY rv.created_at DESC
        LIMIT 500
        """
    ).fetchall()
    st.dataframe(pd.DataFrame([dict(r) for r in rows]) if rows else pd.DataFrame(), width="stretch", hide_index=True)
    con.close()

    
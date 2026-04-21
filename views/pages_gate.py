from __future__ import annotations

import re
import urllib.parse
import os
import uuid
import pandas as pd
import streamlit as st

from auth.auth_service import require_role, current_user
from db.core import db_connect, now_tz, UPLOAD_DIR, ensure_dirs
from services.config import get_config
from services.resident_portal import expire_pending_requests, approve_request, reject_request
from services.audit import audit
from services.parking import (
    normalize_plate,
    plate_is_valid,
    get_resident_vehicle,
    get_active_ticket_by_plate,
    get_apartment,
    get_apartment_by_id,
    private_cells_for_apt,
    private_cell_occupancy,
    policy_capacity,
    can_fit_private,
    active_sanctions,
    best_zone,
    VEHICLE_TYPES,
    level_badge,
    apartment_contact,
)


from views.components import st_df


# -----------------------------
# Helpers UI
# -----------------------------
def _reset_gate_in_form():
    keys = [
        "GIN_plate",
        "GIN_vtype",
        "GIN_vtype_auto",
        "GIN_last_plate",
        "GIN_tower",
        "GIN_apt_pick",
        "GIN_apt_manual",
        "GIN_cell",
        "GIN_zone",
        "GIN_notes",
        "GIN_zone_ctx",
        "GIN_zone_choice",
    ]
    for k in keys:
        st.session_state.pop(k, None)
    st.session_state["_GIN_cleared"] = True


def _toast_if_cleared():
    if st.session_state.pop("_GIN_cleared", False):
        try:
            st.toast("Formulario limpiado ✅")
        except Exception:
            st.success("Formulario limpiado ✅")


def guess_vehicle_type_from_plate(plate: str) -> str:
    """
    Heurística Colombia:
    - Moto típico: ABC12D (3 letras + 2 números + 1 letra)
    - Carro típico: ABC123 (3 letras + 3 números)
    Si no coincide, por defecto CAR.
    """
    p = normalize_plate(plate or "")
    if len(p) == 6 and p[:3].isalpha() and p[3:5].isdigit() and p[5].isalpha():
        return "MOTO"
    return "CAR"


def _on_type_change():
    # si el portero cambia manualmente, dejamos de auto-forzar
    st.session_state["GIN_vtype_auto"] = False


def _normalize_wa(phone: str) -> str:
    digits = re.sub(r"\D+", "", phone or "")
    if digits.startswith("0"):
        digits = digits.lstrip("0")
    # Colombia: si escriben 10 dígitos, asumimos +57
    if len(digits) == 10:
        digits = "57" + digits
    return digits


def _wa_url(phone: str, message: str) -> str:
    p = _normalize_wa(phone)
    if not p:
        return ""
    return f"https://wa.me/{p}?text={urllib.parse.quote(message)}"


def _save_evidence(uploaded_file) -> str | None:
    """Guarda evidencia en uploads/ y retorna la ruta. Si no hay archivo, retorna None."""
    if uploaded_file is None:
        return None
    try:
        ensure_dirs()
        ext = os.path.splitext(getattr(uploaded_file, "name", "") or "")[1].lower() or ".jpg"
        fname = f"{uuid.uuid4().hex}{ext}"
        path = os.path.join(UPLOAD_DIR, fname)
        with open(path, "wb") as f:
            f.write(uploaded_file.getbuffer())
        return path
    except Exception:
        return None


# -----------------------------
# PORTERIA: INGRESO
# -----------------------------
def page_gate_in():
    require_role("PORTERO", "ADMIN")
    con = db_connect()

    _toast_if_cleared()

    # Header + limpiar
    h1, h2 = st.columns([3, 1])
    with h1:
        st.subheader("🟢 Ingreso (Portería)")
        st.caption("Registro rápido. Si la placa existe como residente, se detecta automáticamente.")
    with h2:
        if st.button("🧹 Limpiar", key="GIN_clear_btn", type="secondary"):
            _reset_gate_in_form()
            st.rerun()

    # Aviso cierre del día
    end_h = int(get_config(con, "end_day_notice_hour", 21))
    end_m = int(get_config(con, "end_day_notice_minute", 30))
    n = now_tz()
    if (n.hour > end_h) or (n.hour == end_h and n.minute >= end_m):
        pending = int(
            con.execute(
                "SELECT COUNT(*) as n FROM tickets WHERE ticket_type='VISITOR' AND exit_time IS NULL"
            ).fetchone()["n"]
        )
        if pending > 0:
            st.warning(f"⚠️ Hay **{pending}** visitante(s) sin salida. Revisa **Portería · Cierre del día**.")

    # 1) Placa
    st.markdown("### 1) Placa")
    plate_raw = st.text_input("Placa", placeholder="ABC123", key="GIN_plate")
    plate = normalize_plate(plate_raw)

    if plate_raw and not plate_is_valid(plate_raw):
        st.error("Placa inválida (usa letras/números, sin espacios).")
        con.close()
        return

    # Evitar duplicado (ya dentro)
    if plate and plate_is_valid(plate):
        act = get_active_ticket_by_plate(con, plate)
        if act:
            st.error("⛔ Esta placa ya está registrada como **DENTRO** (sin salida). Registra salida primero.")
            con.close()
            return

    # Detectar residente
    resident = get_resident_vehicle(con, plate) if plate and plate_is_valid(plate) else None
    ticket_type = "RESIDENT" if resident else "VISITOR"

    # 2) Tipo y perfil
    st.markdown("### 2) Tipo y perfil")

    c1, c2, c3 = st.columns([1.2, 1.2, 2.2])
    with c1:
        st.write("**Perfil**")
        st.write("🟦 Residente" if ticket_type == "RESIDENT" else "🟧 Visitante")

    # Auto tipo (maneja session_state para que sí se vea reflejado)
    last_plate = st.session_state.get("GIN_last_plate")
    auto_flag = st.session_state.get("GIN_vtype_auto", True)

    if ticket_type == "RESIDENT":
        auto_type = resident["vehicle_type"]
        st.session_state["GIN_vtype"] = auto_type
        st.session_state["GIN_vtype_auto"] = True
        st.session_state["GIN_last_plate"] = plate
    else:
        if plate and plate_is_valid(plate):
            auto_type = guess_vehicle_type_from_plate(plate)
            if last_plate != plate:
                st.session_state["GIN_last_plate"] = plate
                st.session_state["GIN_vtype_auto"] = True
                st.session_state["GIN_vtype"] = auto_type
            else:
                if auto_flag and "GIN_vtype" in st.session_state:
                    st.session_state["GIN_vtype"] = auto_type
        else:
            if "GIN_vtype" not in st.session_state:
                st.session_state["GIN_vtype"] = "CAR"

    with c2:
        vehicle_type = st.selectbox("Tipo", VEHICLE_TYPES, key="GIN_vtype", on_change=_on_type_change)

    with c3:
        if ticket_type == "RESIDENT":
            st.success("Detectado como residente ✅")
        else:
            if plate and plate_is_valid(plate):
                st.caption("Tipo sugerido automáticamente por placa (puedes cambiarlo).")
            st.caption("Visitante: asociar a apartamento.")

    # 3) Destino
    apt = None

    if ticket_type == "RESIDENT":
        apt = get_apartment_by_id(con, int(resident["apt_id"]))
        if not apt:
            st.error("El apto/torre del residente está inactivo o no existe. Admin debe revisar.")
            con.close()
            return
        st.markdown("### 3) Destino (automático)")
        st.success(f"T{apt['tower_num']}-{apt['apt_number']} · Etapa {apt['etapa_residencial']}")
    else:
        st.markdown("### 3) Destino (Apartamento)")
        towers = con.execute("SELECT * FROM towers WHERE is_active=1 ORDER BY tower_num").fetchall()
        if not towers:
            st.error("No hay torres activas. (Admin → Estructura)")
            con.close()
            return

        colT, colA = st.columns(2)
        with colT:
            tower_options = ["Seleccione..."] + [int(t["tower_num"]) for t in towers]
            tower_sel = st.selectbox("Torre", tower_options, index=0, key="GIN_tower")

            tower_num = None
            if tower_sel != "Seleccione...":
                tower_num = int(tower_sel)

        apt_rows = []
        tower_id_row = None

        if tower_num is not None:
            tower_id_row = con.execute(
                "SELECT id FROM towers WHERE tower_num=? AND is_active=1",
                (int(tower_num),),
            ).fetchone()

            if tower_id_row:
                apt_rows = con.execute(
                    "SELECT apt_number, resident_name, whatsapp FROM apartments WHERE tower_id=? AND is_active=1 ORDER BY apt_number",
                    (int(tower_id_row["id"]),),
                ).fetchall()

        with colA:
            if apt_rows:
                apt_opts = [r["apt_number"] for r in apt_rows]
                apt_pick = st.selectbox("Apto", apt_opts, key="GIN_apt_pick")
                r = next((x for x in apt_rows if x["apt_number"] == apt_pick), None)
                if r and r["resident_name"]:
                    st.caption(f"Residente: {r['resident_name']}")
                apt_number = apt_pick
            else:
                apt_number = st.text_input("Apto", placeholder="922", key="GIN_apt_manual")

        if tower_num is not None and apt_number:
            apt = get_apartment(con, int(tower_num), apt_number.strip())
            if apt:
                st.success(
                    f"Destino: Torre {apt['tower_num']} · Apto {apt['apt_number']} · Etapa {apt['etapa_residencial']}"
                )
            else:
                st.warning("Apartamento no encontrado o inactivo.")

        if tower_num is None:
            st.caption("Selecciona una torre para listar apartamentos.")
            con.close()
            return

    if not apt:
        st.caption("Completa el destino para continuar.")
        con.close()
        return

    # ✅ Validación rápida (compacta)
    st.markdown("### ✅ Validación rápida")

    contact_name, contact_wa = apartment_contact(apt, "VISITOR")

    line_left, line_right = st.columns([2, 1])
    with line_left:
        st.markdown(
            f"**Placa:** `{plate}`  ·  **Destino:** T{apt['tower_num']}-{apt['apt_number']}  ·  **Etapa:** {apt['etapa_residencial']}"
        )
    with line_right:
        if contact_name:
            st.markdown(f"**Contacto:** {contact_name}")

    chips = [f"**Tipo:** {vehicle_type}"]
    if resident and resident["vehicle_type"] != vehicle_type:
        chips.append("⚠️ **Tipo NO coincide**")
    is_pmr_auth = int(resident["is_pmr_authorized"]) if resident and "is_pmr_authorized" in resident.keys() else 0
    if resident and is_pmr_auth == 1:
        chips.append("♿ **PMR**")
    st.info(" · ".join(chips))

    # ✅ Botón anunciar visitante
    if ticket_type == "VISITOR" and contact_wa:
        vtxt = "moto" if vehicle_type == "MOTO" else "carro"
        msg = (
            f"Hola {contact_name or ''}. En portería de Primitiva Parque Natural hay un visitante en {vtxt} "
            f"con placa {plate}. ¿Autorizas el ingreso a tu apartamento Torre {apt['tower_num']}, Apto {apt['apt_number']}? "
            "Responde SI o NO. Gracias."
        ).strip()

        url = _wa_url(contact_wa, msg)
        if url:
            try:
                st.link_button("📲 Anunciar visitante por WhatsApp", url, type="primary")
            except Exception:
                st.markdown(f"[📲 Anunciar visitante por WhatsApp]({url})")

    # 4) Sanciones / bloqueos
    sanc = active_sanctions(con, int(apt["id"]), plate)
    if sanc:
        st.warning("⚠️ Sanciones activas detectadas para este apartamento o placa.")
        block = any(int(s["block_entry"]) == 1 for s in sanc)

        with st.expander("Ver sanciones", expanded=False):
            for s in sanc:
                st.write(
                    f"- **{s['sanction_type']}** · Bloquea: {'Sí' if int(s['block_entry'])==1 else 'No'} · {s['description'] or ''}"
                )

        if block:
            st.error("⛔ BLOQUEO: No puede ingresar hasta paz y salvo.")
            con.close()
            return

    # 5) Asignación
    st.markdown("### 4) Asignación")

    assignment_kind = None
    zone_id = None
    private_cell_id = None
    assignment_label = ""

    has_private = bool(int(apt["has_private_parking"]))
    cells = private_cells_for_apt(con, int(apt["id"])) if (ticket_type == "RESIDENT" and has_private) else []

    if ticket_type == "RESIDENT" and cells:
        st.info("Residente con celdas privadas configuradas. Selecciona la celda.")
        by_code = {c["code"]: c for c in cells}
        code = st.selectbox("Celda privada", list(by_code.keys()), key="GIN_cell")
        cell = by_code[code]
        cars_in, motos_in = private_cell_occupancy(con, int(cell["id"]))
        max_c, max_m = policy_capacity(cell["policy"])
        st.caption(f"Política: {cell['policy']} · Ahora: Carros {cars_in}/{max_c} · Motos {motos_in}/{max_m}")

        if int(cell["is_pmr"]) == 1 and not (resident and int(resident["is_pmr_authorized"]) == 1):
            st.error("⛔ Celda PMR: vehículo NO autorizado.")
            con.close()
            return

        if not can_fit_private(cell["policy"], cars_in, motos_in, vehicle_type):
            st.error("⛔ No cabe según política/ocupación de la celda.")
            con.close()
            return

        assignment_kind = "PRIVATE"
        private_cell_id = int(cell["id"])
        assignment_label = f"Celda {cell['code']}"

    else:
        profile = "VISITOR" if ticket_type == "VISITOR" else "RESIDENT_NO_PRIVATE"
        st.caption("Sugerencia automática según torre y tipo (cercanía configurada).")

        best, opts = best_zone(con, int(apt["tower_id"]), vehicle_type, profile)
        if not opts:
            st.error("No hay zonas configuradas para esa torre/tipo. (Admin → Estructura → Zonas ↔ Torres)")
            con.close()
            return

        best_id = int(best["id"]) if best else int(opts[0]["id"])

        # -----------------------------
        # Selección rápida (botones) - más cómoda en celular
        # -----------------------------
        ctx = f"{int(apt['tower_id'])}|{vehicle_type}|{profile}"
        if st.session_state.get("GIN_zone_ctx") != ctx:
            st.session_state["GIN_zone_ctx"] = ctx
            st.session_state["GIN_zone_choice"] = best_id

        if "GIN_zone_choice" not in st.session_state:
            st.session_state["GIN_zone_choice"] = best_id

        valid_ids = [int(o["id"]) for o in opts]
        if int(st.session_state.get("GIN_zone_choice", best_id)) not in valid_ids:
            st.session_state["GIN_zone_choice"] = best_id

        zone_names = {int(o["id"]): o["name"] for o in opts}
        selected = int(st.session_state["GIN_zone_choice"])

        def _pick_btn(label: str, key: str, primary: bool):
            try:
                return st.button(label, key=key, type=("primary" if primary else "secondary"))
            except TypeError:
                return st.button(label, key=key)

        def _render_zone_pick(o):
            zid = int(o["id"])
            is_sel = zid == selected
            badge = level_badge(int(o["lvl"]))
            label = f"{badge} {o['name']} · Disp {int(o['avail'])} · Ocup {int(o['used'])}"
            btn_text = f"✅ {label}" if is_sel else f"{label} · Seleccionar"
            if _pick_btn(btn_text, key=f"GIN_zone_pick_{zid}", primary=is_sel):
                st.session_state["GIN_zone_choice"] = zid
                st.rerun()

        st.markdown("#### Zonas sugeridas (toca para seleccionar)")
        shown = opts[:6]
        extra = opts[6:]

        for o in shown:
            _render_zone_pick(o)

        if extra:
            with st.expander("Ver más zonas"):
                for o in extra:
                    _render_zone_pick(o)

        chosen = int(st.session_state["GIN_zone_choice"])

        zrow = con.execute("SELECT * FROM zones WHERE id=? AND is_active=1", (chosen,)).fetchone()
        if not zrow:
            st.error("Zona inactiva. Selecciona otra.")
            con.close()
            return

        assignment_kind = "ZONE"
        zone_id = chosen
        assignment_label = f"Zona {zone_names[chosen]}"

    # 6) Confirmar
    st.markdown("### 5) Confirmar")
    notes = st.text_area("Notas (opcional)", placeholder="Ej: autorización, observaciones...", key="GIN_notes")

    can_save = bool(plate and plate_is_valid(plate) and assignment_kind)
    clicked = st.button("✅ Registrar ingreso", disabled=not can_save, key="GIN_save_btn", type="primary")

    if clicked and can_save:
        u = current_user()
        assert u is not None

        cur = con.execute(
            """
            INSERT INTO tickets(plate,vehicle_type,ticket_type,apt_id,zone_id,private_cell_id,entry_time,entry_by,exit_mode,notes)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                plate,
                vehicle_type,
                ticket_type,
                int(apt["id"]),
                zone_id,
                private_cell_id,
                now_tz().isoformat(),
                int(u.id),
                None,
                notes.strip() if notes else None,
            ),
        )
        ticket_id = int(cur.lastrowid)

        # Si viene de una solicitud del portal, la marcamos como APROBADA
        req_id = st.session_state.pop("_portal_entry_req_id", None)
        if req_id:
            try:
                approve_request(con, int(req_id), int(u.id), ticket_id=ticket_id, notes="Ingreso aprobado por portería")
                audit("RESIDENT_ENTRY_APPROVE", u.id, {"plate": plate, "request_id": int(req_id), "ticket_id": ticket_id})
            except Exception:
                pass

        con.commit()

        audit("GATE_IN", u.id, {"plate": plate, "type": vehicle_type, "ticket_type": ticket_type, "assignment": assignment_label})
        st.success(f"Ingreso registrado ✅ · {plate} → {assignment_label}")

        _reset_gate_in_form()
        st.session_state["GIN_plate"] = ""
        st.rerun()

    con.close()


# -----------------------------
# PORTERIA: CONTROL (DENTRO + SALIDA)
# -----------------------------
def page_gate_control():
    """
    Portería · Control (Dentro + Salida) — vista unificada y móvil-friendly.
    - Buscar placa (filtra lista)
    - Filtros opcionales (Perfil/Tipo/Límite)
    - Lista de vehículos dentro (tarjetas + tabla opcional)
    - Panel de salida con confirmación (solo PORTERO/ADMIN)
    """
    require_role("PORTERO", "ADMIN", "AUDITOR")
    con = db_connect()
    u = current_user()
    assert u is not None

    # Reset seguro (antes de widgets)
    if st.session_state.pop("_CTRL_reset", False):
        for k in [
            "CTRL_plate",
            "CTRL_confirm",
            "_CTRL_selected_plate",
            "CTRL_prof",
            "CTRL_types",
            "CTRL_limit",
            "CTRL_inc_desc",
            "CTRL_inc_confirm",
            "CTRL_inc_file",
        ]:
            st.session_state.pop(k, None)

    done_plate = st.session_state.pop("_CTRL_done_plate", None)
    if done_plate:
        try:
            st.toast(f"Salida registrada ✅ · {done_plate}")
        except Exception:
            st.success(f"Salida registrada ✅ · {done_plate}")

    done_inc = st.session_state.pop("_CTRL_done_incident", None)
    if done_inc:
        try:
            st.toast(f"Incidencia enviada a auditoría ✅ · {done_inc}")
        except Exception:
            st.success(f"Incidencia enviada a auditoría ✅ · {done_inc}")


    st.subheader("🧩 Portería · Control (Dentro + Salida)")

    # -------------------------
    # Portal residentes: solicitudes de salida (opcional)
    # -------------------------
    if bool(get_config(con, "resident_portal_enabled", False)) and bool(get_config(con, "resident_portal_allow_exit", True)):
        expire_pending_requests(con)
        con.commit()
        with st.expander("🟦 Solicitudes de salida (residentes)", expanded=True):
            st.caption("Aprueba y registra la salida con un clic (si el vehículo está dentro).")
            now_iso = now_tz().isoformat()
            reqs = con.execute(
                """
                SELECT r.id, r.plate, r.requested_at, r.expires_at,
                       t.id as ticket_id, t.entry_time,
                       tw.tower_num, ap.apt_number
                FROM resident_portal_requests r
                LEFT JOIN tickets t ON t.plate=r.plate AND t.exit_time IS NULL
                LEFT JOIN resident_vehicles rv ON rv.plate=r.plate AND rv.is_active=1
                LEFT JOIN apartments ap ON ap.id=rv.apt_id
                LEFT JOIN towers tw ON tw.id=ap.tower_id
                WHERE r.status='PENDING' AND r.request_type='EXIT' AND r.expires_at >= ?
                ORDER BY r.requested_at DESC
                LIMIT 30
                """,
                (now_iso,),
            ).fetchall()

            if not reqs:
                st.caption("Sin solicitudes de salida pendientes.")
            else:
                for rr in reqs:
                    plate2 = rr["plate"]
                    apt_txt = f"T{int(rr['tower_num'])}-{rr['apt_number']}" if rr["tower_num"] is not None else "(sin apto)"
                    entry_txt = str(rr["entry_time"])[:16] if rr["entry_time"] else "-"
                    left, right = st.columns([4, 1])
                    with left:
                        st.write(f"**{plate2}** · {apt_txt} · dentro desde {entry_txt} · vence {str(rr['expires_at'])[:16]}")
                    with right:
                        if st.button("Aceptar", key=f"gctrl_exit_accept_{int(rr['id'])}"):
                            tk = con.execute(
                                "SELECT id FROM tickets WHERE plate=? AND exit_time IS NULL ORDER BY entry_time DESC LIMIT 1",
                                (plate2,),
                            ).fetchone()

                            if not tk:
                                reject_request(con, int(rr["id"]), int(u.id), notes="No hay ticket abierto para esta placa")
                                con.commit()
                                st.warning("No hay ticket abierto para esa placa.")
                                st.rerun()

                        con.execute(
                            """
                            UPDATE tickets
                            SET exit_time=?, exited_by=?, exit_mode=?
                            WHERE id=?
                            """,
                            (now_tz().isoformat(), int(u.id), "RESIDENT_REQUEST", int(tk["id"])),
                        )
                        approve_request(con, int(rr["id"]), int(u.id), ticket_id=int(tk["id"]), notes="Salida aprobada por portería")
                        audit("GATE_OUT", u.id, {"plate": plate2, "ticket_id": int(tk["id"]), "exit_mode": "RESIDENT_REQUEST"})
                        audit("RESIDENT_EXIT_APPROVE", u.id, {"plate": plate2, "request_id": int(rr["id"]), "ticket_id": int(tk["id"])})
                        con.commit()
                        st.success("Salida registrada ✅")
                        st.rerun()

    st.caption("Busca por placa y gestiona salidas sin cambiar de pantalla.")

    # Buscar placa
    plate_raw = st.text_input("Buscar placa", placeholder="ABC123", key="CTRL_plate")
    plate_q = normalize_plate(plate_raw)

    # Filtros en expander para móvil
    with st.expander("Filtros (opcional)", expanded=False):
        prof = st.multiselect(
            "Perfil",
            ["RESIDENT", "VISITOR"],
            default=["RESIDENT", "VISITOR"],
            key="CTRL_prof",
        )
        vtypes = st.multiselect(
            "Tipo",
            ["CAR", "MOTO"],
            default=["CAR", "MOTO"],
            key="CTRL_types",
        )
        limit = st.slider("Máximo a cargar", 50, 400, 200, key="CTRL_limit")

    # Query activos con filtros
    sql = """
        SELECT
            tk.id as ticket_id,
            tk.plate,
            tk.vehicle_type,
            tk.ticket_type,
            tk.entry_time,
            t.etapa_residencial,
            t.tower_num,
            a.apt_number,
            COALESCE(pc.code, z.name, '-') as place
        FROM tickets tk
        JOIN apartments a ON a.id=tk.apt_id
        JOIN towers t ON t.id=a.tower_id
        LEFT JOIN zones z ON z.id=tk.zone_id
        LEFT JOIN private_cells pc ON pc.id=tk.private_cell_id
        WHERE tk.exit_time IS NULL
    """
    params = []

    if prof:
        sql += " AND tk.ticket_type IN ({})".format(",".join(["?"] * len(prof)))
        params += prof
    if vtypes:
        sql += " AND tk.vehicle_type IN ({})".format(",".join(["?"] * len(vtypes)))
        params += vtypes

    sql += """
        ORDER BY
          CASE tk.ticket_type WHEN 'VISITOR' THEN 0 ELSE 1 END,
          tk.entry_time DESC
        LIMIT ?
    """
    params.append(int(limit))

    rows = con.execute(sql, tuple(params)).fetchall()

    # Filtrado por placa mientras escribe (prefijo)
    if plate_q:
        rows = [r for r in rows if normalize_plate(r["plate"]).startswith(plate_q)]

    # Métricas rápidas
    total = len(rows)
    cars = sum(1 for r in rows if r["vehicle_type"] == "CAR")
    motos = sum(1 for r in rows if r["vehicle_type"] == "MOTO")

    c1, c2, c3 = st.columns(3)
    c1.metric("Dentro", total)
    c2.metric("🚗 Carros", cars)
    c3.metric("🏍️ Motos", motos)

    selected_plate = st.session_state.get("_CTRL_selected_plate")

    # Si escribieron placa completa válida y está activa → seleccionamos
    if plate_raw and plate_is_valid(plate_raw):
        active = get_active_ticket_by_plate(con, normalize_plate(plate_raw))
        if active:
            st.session_state["_CTRL_selected_plate"] = normalize_plate(plate_raw)
            selected_plate = normalize_plate(plate_raw)

    # Panel salida (solo PORTERO/ADMIN)
    if selected_plate:
        tk = con.execute(
            """
            SELECT
                tk.id as ticket_id,
                tk.plate,
                tk.vehicle_type,
                tk.ticket_type,
                tk.entry_time,
                t.tower_num,
                a.apt_number,
                COALESCE(pc.code, z.name, '-') as place
            FROM tickets tk
            JOIN apartments a ON a.id=tk.apt_id
            JOIN towers t ON t.id=a.tower_id
            LEFT JOIN zones z ON z.id=tk.zone_id
            LEFT JOIN private_cells pc ON pc.id=tk.private_cell_id
            WHERE tk.exit_time IS NULL AND tk.plate=?
            """,
            (selected_plate,),
        ).fetchone()

        if tk:
            st.markdown("### ✅ Confirmar salida")
            with st.container(border=True):
                st.markdown(f"**Placa:** `{tk['plate']}` · **Tipo:** {tk['vehicle_type']} · **Perfil:** {tk['ticket_type']}")
                st.markdown(f"**Destino:** T{tk['tower_num']}-{tk['apt_number']}")
                st.markdown(f"**Lugar:** {tk['place']}")
                st.caption(f"Entrada: {str(tk['entry_time'])[:16]}")

                if u.role in ("PORTERO", "ADMIN"):
                    confirm = st.checkbox(
                        "Confirmo que el vehículo salió (visto en cámaras / salida física).",
                        key="CTRL_confirm",
                    )
                    colA, colB = st.columns([2, 1])
                    with colA:
                        clicked = st.button("✅ Registrar salida", type="primary", disabled=not confirm, key="CTRL_save_btn")
                    with colB:
                        if st.button("Limpiar selección", key="CTRL_clear_sel"):
                            st.session_state["_CTRL_reset"] = True
                            st.rerun()

                    if clicked and confirm:
                        con.execute(
                            "UPDATE tickets SET exit_time=?, exit_by=?, exit_mode=? WHERE id=?",
                            (now_tz().isoformat(), int(u.id), "NORMAL", int(tk["ticket_id"])),
                        )
                        con.commit()
                        audit("GATE_OUT", u.id, {"plate": tk["plate"], "ticket_id": int(tk["ticket_id"])})

                        st.session_state["_CTRL_done_plate"] = tk["plate"]
                        st.session_state["_CTRL_reset"] = True
                        st.rerun()


                    # ---- Incidencia (escalamiento a auditoría) ----
                    st.markdown("---")
                    st.markdown("#### 🚩 Reportar incidencia (enviar a auditoría)")

                    ex_inc = con.execute(
                        "SELECT id, created_at FROM incidents WHERE status='OPEN' AND ticket_id=? ORDER BY id DESC LIMIT 1",
                        (int(tk["ticket_id"]),),
                    ).fetchone()

                    if ex_inc:
                        st.success(f"✅ Ya existe incidencia abierta: #{int(ex_inc['id'])} · {str(ex_inc['created_at'])[:16]}")
                    else:
                        desc = st.text_area(
                            "Detalle de la incidencia",
                            placeholder="Ej: visitante se estacionó en lugar indebido / no responde / conducta irregular…",
                            key="CTRL_inc_desc",
                        )
                        ev = st.file_uploader(
                            "Evidencia (opcional)",
                            type=["png", "jpg", "jpeg", "webp"],
                            key="CTRL_inc_file",
                        )
                        confirm_i = st.checkbox(
                            "Confirmo enviar esta incidencia a Auditoría (queda PENDIENTE).",
                            key="CTRL_inc_confirm",
                        )

                        send = st.button(
                            "🚩 Enviar incidencia",
                            key="CTRL_inc_send",
                            disabled=(not confirm_i or not (desc or "").strip()),
                        )

                        if send and confirm_i and (desc or "").strip():
                            evidence_path = _save_evidence(ev)

                            con.execute(
                                """
                                INSERT INTO incidents(
                                    ticket_id, apt_id, plate, vehicle_type, ticket_type,
                                    zone_id, private_cell_id,
                                    description, evidence_path,
                                    status, created_at, created_by
                                )
                                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                                """,
                                (
                                    int(tk["ticket_id"]),
                                    int(tk["apt_id"]),
                                    tk["plate"],
                                    tk["vehicle_type"],
                                    tk["ticket_type"],
                                    int(tk["zone_id"]) if tk["zone_id"] is not None else None,
                                    int(tk["private_cell_id"]) if tk["private_cell_id"] is not None else None,
                                    (desc or "").strip(),
                                    evidence_path,
                                    "OPEN",
                                    now_tz().isoformat(),
                                    int(u.id),
                                ),
                            )
                            con.commit()
                            audit("INCIDENT_CREATE", u.id, {"ticket_id": int(tk["ticket_id"]), "plate": tk["plate"]})

                            st.session_state["_CTRL_done_incident"] = tk["plate"]
                            st.session_state["_CTRL_reset"] = True
                            st.rerun()

                else:
                    st.info("Modo consulta (AUDITOR): no puedes registrar salidas.")
        else:
            st.info("La placa seleccionada ya no está activa (puede que ya haya salido).")

    # Lista móvil (tarjetas)
    st.markdown("### Lista rápida (móvil)")
    if not rows:
        st.info("No hay vehículos dentro con esos filtros.")
        con.close()
        return

    for r in rows[:12]:
        with st.container(border=True):
            left, right = st.columns([2, 1])
            with left:
                st.markdown(f"**{r['plate']}** · {r['vehicle_type']} · {r['ticket_type']}")
                st.caption(f"Etapa {int(r['etapa_residencial'])} · T{r['tower_num']}-{r['apt_number']} · {r['place']}")
            with right:
                st.caption(str(r["entry_time"])[:16])
                if st.button("Seleccionar", key=f"CTRL_sel_{r['plate']}"):
                    st.session_state["_CTRL_selected_plate"] = r["plate"]
                    st.rerun()

    # Tabla opcional
    with st.expander("Ver tabla (opcional)", expanded=False):
        df = pd.DataFrame(
            [
                {
                    "Placa": r["plate"],
                    "Tipo": r["vehicle_type"],
                    "Perfil": r["ticket_type"],
                    "Etapa": int(r["etapa_residencial"]),
                    "Apto": f"T{r['tower_num']}-{r['apt_number']}",
                    "Lugar": r["place"],
                    "Entrada": str(r["entry_time"])[:16],
                }
                for r in rows[:200]
            ]
        )
        st_df(df)

    con.close()


# -----------------------------
# PORTERIA: CIERRE DEL DÍA
# -----------------------------
def page_end_day():
    require_role("PORTERO", "ADMIN")
    con = db_connect()
    st.subheader("🌙 Cierre del día (visitantes sin salida)")

    rows = con.execute(
        """
        SELECT tk.id, tk.plate, tk.vehicle_type, tk.ticket_type,
               tk.apt_id, tk.zone_id, tk.private_cell_id,
               tk.entry_time,
               t.tower_num, a.apt_number, z.name as zone_name,
               a.resident_name as resident_name, a.whatsapp as whatsapp
        FROM tickets tk
        JOIN apartments a ON a.id=tk.apt_id
        JOIN towers t ON t.id=a.tower_id
        LEFT JOIN zones z ON z.id=tk.zone_id
        WHERE tk.exit_time IS NULL AND tk.ticket_type='VISITOR'
        ORDER BY tk.entry_time ASC
        """
    ).fetchall()

    if not rows:
        st.success("No hay visitantes pendientes ✅")
        con.close()
        return

    st.warning(f"Pendientes: {len(rows)}")
    approx = st.time_input(
        "Hora aproximada (si confirmas salida)",
        value=now_tz().time().replace(second=0, microsecond=0),
        key="END_time",
    )

    now_ts = pd.Timestamp(now_tz())

    warn_min = int(get_config(con, "end_day_threshold_warn_min", 60))
    crit_min = int(get_config(con, "end_day_threshold_crit_min", 180))

    def short_dt(s: str) -> str:
        ts = pd.to_datetime(s, errors="coerce")
        if pd.isna(ts):
            return str(s)[:16]
        return ts.strftime("%Y-%m-%dT%H:%M")

    def dur_text_and_emoji(s: str):
        ts = pd.to_datetime(s, errors="coerce")
        if pd.isna(ts):
            return ("", "⚪")
        delta = now_ts - ts
        mins = int(delta.total_seconds() // 60)
        h = mins // 60
        m = mins % 60

        if mins >= crit_min:
            emo = "🔴"
        elif mins >= warn_min:
            emo = "🟠"
        else:
            emo = "🟢"

        return (f"{h}h {m:02d}m", emo)

    # ✅ Pre-cargar incidencias abiertas para marcar/evitar duplicados
    ticket_ids = [int(r["id"]) for r in rows]
    open_inc_ticket_ids = set()
    if ticket_ids:
        placeholders = ",".join(["?"] * len(ticket_ids))
        inc_rows = con.execute(
            f"SELECT ticket_id FROM incidents WHERE status='OPEN' AND ticket_id IN ({placeholders})",
            tuple(ticket_ids),
        ).fetchall()
        open_inc_ticket_ids = {int(x["ticket_id"]) for x in inc_rows}

    for r in rows:
        dur, emo = dur_text_and_emoji(r["entry_time"])
        entry_short = short_dt(r["entry_time"])

        title = f"{emo} {r['plate']} · T{r['tower_num']}-{r['apt_number']} · {entry_short}"
        if dur:
            title += f" · ⏱ {dur}"

        with st.expander(title, expanded=False):
            st.write("Zona:", r["zone_name"] or "-")
            if dur:
                st.caption(f"⏱ Tiempo dentro: **{dur}**")

            # ✅ WhatsApp al residente/propietario (si está)
            resident_name = (r["resident_name"] or "").strip()
            whatsapp = (r["whatsapp"] or "").strip()

            if whatsapp:
                tipo_txt = "moto" if (r["vehicle_type"] == "MOTO") else "carro"
                saludo = f"Hola {resident_name}," if resident_name else "Hola,"
                msg = (
                    f"{saludo} te escribimos desde portería de Primitiva Parque Natural.\n\n"
                    f"Tenemos registrado un visitante en {tipo_txt} con placa {r['plate']} "
                    f"asociado a tu apartamento Torre {r['tower_num']}, Apto {r['apt_number']}.\n"
                    f"Zona: {r['zone_name'] or '-'}\n"
                    f"Hora ingreso: {entry_short}\n"
                    f"Tiempo dentro: {dur or 'N/D'}\n\n"
                    "¿Nos confirmas si ya salió? Si aún está dentro, por favor solicitarle que retire el vehículo.\n"
                    "¡Gracias!"
                )
                url = _wa_url(whatsapp, msg)
                if url:
                    try:
                        st.link_button("📲 Avisar por WhatsApp", url, type="primary")
                    except Exception:
                        st.markdown(f"[📲 Avisar por WhatsApp]({url})")
            else:
                st.info("Este apartamento no tiene WhatsApp registrado (Admin → Estructura → Apartamentos).")

            c1, c2 = st.columns(2)
            with c1:
                if st.button("✅ Confirmar salida", key=f"END_c_{r['id']}"):
                    u = current_user()
                    assert u is not None
                    note = f"\nConfirmada en cierre del día. Hora aprox: {approx.strftime('%H:%M')}."
                    con.execute(
                        "UPDATE tickets SET exit_time=?, exit_by=?, exit_mode=?, notes=COALESCE(notes,'')||? WHERE id=?",
                        (now_tz().isoformat(), int(u.id), "END_OF_DAY_CONFIRMATION", note, int(r["id"])),
                    )
                    con.commit()
                    audit("END_DAY_CONFIRM_EXIT", u.id, {"ticket_id": int(r["id"]), "plate": r["plate"]})
                    st.success("Confirmado ✅")
                    st.rerun()

            with c2:
                already = int(r["id"]) in open_inc_ticket_ids
                if already:
                    st.caption("✅ Incidencia ya reportada")

                if st.button("🚩 Incidencia", key=f"END_i_{r['id']}", disabled=already):
                    u = current_user()
                    assert u is not None

                    desc = (
                        "Incidencia reportada desde Cierre del día: visitante sin salida. "
                        f"Zona: {r['zone_name'] or '-'} · "
                        f"Ingreso: {entry_short} · "
                        f"Tiempo dentro: {dur or 'N/D'}."
                    )

                    con.execute(
                        """
                        INSERT INTO incidents(
                            ticket_id, plate, vehicle_type, ticket_type,
                            apt_id, zone_id, private_cell_id,
                            incident_type, description, status,
                            created_at, created_by
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            int(r["id"]),
                            r["plate"],
                            r["vehicle_type"],
                            r["ticket_type"],
                            int(r["apt_id"]),
                            int(r["zone_id"]) if r["zone_id"] is not None else None,
                            int(r["private_cell_id"]) if r["private_cell_id"] is not None else None,
                            "END_DAY",
                            desc,
                            "OPEN",
                            now_tz().isoformat(),
                            int(u.id),
                        ),
                    )
                    con.commit()

                    audit("END_DAY_INCIDENT", u.id, {"ticket_id": int(r["id"]), "plate": r["plate"]})
                    st.success("Incidencia enviada a Auditoría ✅")
                    st.rerun()

    con.close()
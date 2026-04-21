from __future__ import annotations

from datetime import timedelta
from typing import Any, Dict, List, Optional, Tuple

import io
import os
import re
import urllib.parse
import uuid

import pandas as pd
import streamlit as st

from auth.auth_service import require_role, current_user
from db.core import db_connect, now_tz, UPLOAD_DIR, ensure_dirs
from services.audit import audit, count_open_incidents
from services.config import get_config
from services.parking import plate_is_valid, normalize_plate, apartment_contact
from views.components import st_df, st_image_safe


# ============================================================
# WhatsApp helpers
# ============================================================

def _normalize_wa(phone: str) -> str:
    """Normaliza WhatsApp a formato wa.me (solo dígitos). Si tiene 10 dígitos, asume Colombia (+57)."""
    digits = re.sub(r"\D+", "", phone or "")
    if digits.startswith("0"):
        digits = digits.lstrip("0")
    if len(digits) == 10:
        digits = "57" + digits
    return digits


def _wa_url(phone: str, message: str) -> str:
    p = _normalize_wa(phone)
    if not p:
        return ""
    return f"https://wa.me/{p}?text={urllib.parse.quote(message)}"


class _SafeFormatDict(dict):
    """Permite template.format_map sin romperse si falta una variable."""
    def __missing__(self, key):
        return "{" + str(key) + "}"


def _render_tpl(tpl: str, ctx: dict) -> str:
    try:
        return str(tpl).format_map(_SafeFormatDict(ctx))
    except Exception:
        return str(tpl)


def _short_dt(s: str) -> str:
    ts = pd.to_datetime(s, errors="coerce")
    if pd.isna(ts):
        return str(s)[:16]
    return ts.strftime("%Y-%m-%dT%H:%M")


# ============================================================
# Safe helpers
# ============================================================

def _table_has_column(con, table: str, col: str) -> bool:
    try:
        cols = con.execute(f"PRAGMA table_info({table})").fetchall()
        # row_factory normalmente es sqlite3.Row; si no, puede venir como tupla
        for r in cols:
            try:
                name = str(r["name"])
            except Exception:
                name = str(r[1]) if len(r) > 1 else ""
            if name.lower() == col.lower():
                return True
        return False
    except Exception:
        return False


def _rowdict(r) -> Dict[str, Any]:
    """Convierte sqlite3.Row (o dict) a dict seguro."""
    if r is None:
        return {}
    if isinstance(r, dict):
        return r
    try:
        return dict(r)
    except Exception:
        try:
            return {k: r[k] for k in r.keys()}  # type: ignore
        except Exception:
            return {}


def _safe_path_exists(p: str) -> bool:
    try:
        return bool(p) and os.path.exists(p)
    except Exception:
        return False


def _incident_repeat_cfg(con) -> Tuple[int, int]:
    """(threshold, window_days) para marcar reiteración y sugerir sanción."""
    thr = int(get_config(con, "incident_repeat_threshold", 3))
    win = int(get_config(con, "incident_repeat_window_days", 30))
    thr = max(thr, 1)
    win = max(win, 1)
    return thr, win


# ============================================================
# PDF helpers (ReportLab optional)
# ============================================================

_HAS_REPORTLAB = False
try:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import cm
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    _HAS_REPORTLAB = True
except Exception:
    _HAS_REPORTLAB = False


def _make_pdf_bytes(title: str, lines: List[Tuple[str, str]], evidence_path: Optional[str] = None) -> Optional[bytes]:
    """Genera PDF A4 simple con líneas key/value y una imagen (si existe)."""
    if not _HAS_REPORTLAB:
        return None

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    w, h = A4

    c.setFont("Helvetica-Bold", 14)
    c.drawString(2 * cm, h - 2 * cm, (title or "")[:120])

    y = h - 3.0 * cm
    for k, v in lines:
        if y < 3 * cm:
            c.showPage()
            y = h - 2 * cm

        c.setFont("Helvetica-Bold", 10)
        c.drawString(2 * cm, y, f"{k}:")
        c.setFont("Helvetica", 10)

        text = (v or "-").replace("\r", " ").replace("\n", " ").strip()
        max_chars = 110
        parts = [text[i:i + max_chars] for i in range(0, len(text), max_chars)] or ["-"]
        for part in parts[:8]:
            if y < 3 * cm:
                c.showPage()
                y = h - 2 * cm
                c.setFont("Helvetica", 10)
            c.drawString(4.2 * cm, y, part)
            y -= 0.55 * cm

        y -= 0.2 * cm

    # Evidence
    if evidence_path and os.path.exists(evidence_path):
        try:
            if y < 11 * cm:
                c.showPage()
                y = h - 2 * cm

            c.setFont("Helvetica-Bold", 11)
            c.drawString(2 * cm, y, "Evidencia")
            y -= 0.8 * cm

            img = ImageReader(evidence_path)
            iw, ih = img.getSize()

            max_w = w - 4 * cm
            max_h = min(12 * cm, y - 2 * cm)
            if max_h < 4 * cm:
                max_h = 10 * cm

            scale = min(max_w / float(iw), max_h / float(ih))
            dw, dh = iw * scale, ih * scale

            c.drawImage(img, 2 * cm, y - dh, width=dw, height=dh, preserveAspectRatio=True, anchor="sw")
        except Exception:
            pass

    c.showPage()
    c.save()
    return buf.getvalue()


def _build_incident_pdf(inc: Dict[str, Any]) -> Optional[bytes]:
    title = f"Incidencia #{inc.get('id','-')} · {inc.get('plate','-')}"
    lines = [
        ("Estado", str(inc.get("status", "-"))),
        ("Fecha reporte", _short_dt(str(inc.get("created_at") or ""))),
        ("Placa", str(inc.get("plate", "-"))),
        ("Tipo", str(inc.get("vehicle_type", "-"))),
        ("Perfil", str(inc.get("ticket_type", "-"))),
        ("Destino", f"T{inc.get('tower_num','-')}-{inc.get('apt_number','-')}"),
        ("Etapa", str(inc.get("etapa_residencial", "-"))),
        ("Lugar", str(inc.get("place", "-"))),
        ("Reportó", str(inc.get("created_by_name", "-"))),
        ("Detalle", str((inc.get("description") or "-")).strip()),
    ]
    if inc.get("status") == "CLOSED":
        lines += [
            ("Cerrada", _short_dt(str(inc.get("closed_at") or ""))),
            ("Cerró", str(inc.get("closed_by_name") or "-")),
            ("Notas cierre", str((inc.get("close_notes") or "-")).strip()),
        ]
    return _make_pdf_bytes(title=title, lines=lines, evidence_path=str(inc.get("evidence_path") or "").strip() or None)


def _build_sanction_pdf(san: Dict[str, Any]) -> Optional[bytes]:
    target = san.get("target") or "-"
    title = f"Sanción #{san.get('id','-')} · {target}"
    lines = [
        ("Estado", str(san.get("status", "-"))),
        ("Fecha creación", _short_dt(str(san.get("created_at") or ""))),
        ("Tipo", str(san.get("sanction_type", "-"))),
        ("Objetivo", str(target)),
        ("Bloquea ingreso", "Sí" if int(san.get("block_entry") or 0) == 1 else "No"),
        ("Valor", str(san.get("amount") or "-")),
        ("Descripción", str((san.get("description") or "-")).strip()),
        ("Creó", str(san.get("created_by_name") or "-")),
    ]
    if san.get("status") == "CLOSED":
        lines += [
            ("Cerrada", _short_dt(str(san.get("closed_at") or ""))),
            ("Cerró", str(san.get("closed_by_name") or "-")),
            ("Motivo cierre", str((san.get("close_reason") or "-")).strip()),
        ]
    return _make_pdf_bytes(title=title, lines=lines, evidence_path=str(san.get("evidence_path") or "").strip() or None)


# ============================================================
# AUDITOR: SANCIONES
# ============================================================

def page_sanctions():
    require_role("AUDITOR", "ADMIN")
    con = db_connect()
    st.subheader("🧾 Sanciones / Multas")

    u = current_user()
    assert u is not None

    # -------------------------
    # Crear sanción
    # -------------------------
    with st.expander("➕ Crear sanción", expanded=True):
        scope = st.selectbox(
            "Aplicar a",
            ["APARTMENT", "PLATE"],
            format_func=lambda x: "Apartamento" if x == "APARTMENT" else "Placa",
            key="SAN_scope",
        )
        apt_id = None
        plate = None

        if scope == "APARTMENT":
            towers = con.execute("SELECT * FROM towers WHERE is_active=1 ORDER BY tower_num").fetchall()
            tower_num = st.selectbox("Torre", [int(t["tower_num"]) for t in towers], key="SAN_tower") if towers else None
            apt_number = st.text_input("Apartamento", placeholder="922", key="SAN_apt")
            if tower_num and apt_number:
                apt = con.execute(
                    """
                    SELECT a.*, t.tower_num, t.etapa_residencial
                    FROM apartments a JOIN towers t ON t.id=a.tower_id
                    WHERE t.tower_num=? AND a.apt_number=? AND a.is_active=1 AND t.is_active=1
                    """,
                    (int(tower_num), apt_number.strip()),
                ).fetchone()
                if apt:
                    apt_id = int(apt["id"])
                    st.success(f"T{apt['tower_num']}-{apt['apt_number']} · Etapa {apt['etapa_residencial']}")
                else:
                    st.warning("Apartamento no encontrado o inactivo.")
        else:
            plate_in = st.text_input("Placa", placeholder="ABC123", key="SAN_plate")
            if plate_in:
                if plate_is_valid(plate_in):
                    plate = normalize_plate(plate_in)
                else:
                    st.error("Placa inválida.")

        sanction_type = st.selectbox(
            "Tipo",
            [
                "Parqueo prohibido",
                "Cebra / zona de circulación",
                "Ocupó celda privada ajena",
                "Uso indebido PMR",
                "Visitante excede tiempo / uso reiterado",
                "Otra",
            ],
            key="SAN_type",
        )
        description = st.text_area("Descripción", placeholder="Describe el hecho…", key="SAN_desc")
        block_entry = st.checkbox("Bloquea ingreso", value=True, key="SAN_block")
        amount = st.number_input("Valor (opcional)", min_value=0.0, step=1000.0, value=0.0, key="SAN_amount")
        evidence = st.file_uploader("Evidencia (foto)", type=["png", "jpg", "jpeg", "webp"], key="SAN_file")

        if st.button("Crear sanción", key="SAN_create", type="primary"):
            if scope == "APARTMENT" and not apt_id:
                st.error("Selecciona un apartamento válido.")
            elif scope == "PLATE" and not plate:
                st.error("Ingresa una placa válida.")
            else:
                path = None
                if evidence is not None:
                    ensure_dirs()
                    ext = os.path.splitext(evidence.name)[1].lower() or ".jpg"
                    fname = f"{uuid.uuid4().hex}{ext}"
                    path = os.path.join(UPLOAD_DIR, fname)
                    with open(path, "wb") as f:
                        f.write(evidence.getbuffer())

                con.execute(
                    """
                    INSERT INTO sanctions(scope,apt_id,plate,sanction_type,description,evidence_path,amount,block_entry,status,created_at,created_by)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        scope,
                        apt_id,
                        plate,
                        sanction_type,
                        description.strip() if description else None,
                        path,
                        float(amount) if amount else None,
                        1 if block_entry else 0,
                        "ACTIVE",
                        now_tz().isoformat(),
                        int(u.id),
                    ),
                )
                con.commit()
                audit("SANCTION_CREATE", u.id, {"scope": scope, "apt_id": apt_id, "plate": plate, "type": sanction_type})
                st.success("Sanción creada ✅")
                st.rerun()

    tab_active, tab_hist = st.tabs(["✅ Activas", "📚 Historial y estadísticas"])

    # -------------------------
    # Activas (ficha tipo incidencias)
    # -------------------------
    with tab_active:
        rows = con.execute(
            """
            SELECT s.*,
                   u.full_name as created_by_name,
                   t.tower_num, a.apt_number,
                   a.resident_name, a.whatsapp,
                   COALESCE(a.is_rented,0) as is_rented,
                   a.owner_name as owner_name, a.owner_whatsapp as owner_whatsapp,
                   a.tenant_name as tenant_name, a.tenant_whatsapp as tenant_whatsapp
            FROM sanctions s
            JOIN users u ON u.id=s.created_by
            LEFT JOIN apartments a ON a.id=s.apt_id
            LEFT JOIN towers t ON t.id=a.tower_id
            WHERE s.status='ACTIVE'
            ORDER BY s.created_at DESC
            """
        ).fetchall()

        if not rows:
            st.info("No hay sanciones activas.")
        else:
            for r0 in rows:
                r = _rowdict(r0)

                target = (
                    f"T{r.get('tower_num','-')}-{r.get('apt_number','-')}"
                    if r.get("scope") == "APARTMENT"
                    else (r.get("plate") or "-")
                )

                title = f"#{r.get('id')} · {r.get('sanction_type','-')} · {target} · {_short_dt(str(r.get('created_at') or ''))}"
                with st.expander(title, expanded=False):
                    left, right = st.columns([2.2, 1.2])

                    with left:
                        st.markdown(f"**Objetivo:** {target}")
                        st.markdown(f"**Bloquea:** {'Sí' if int(r.get('block_entry') or 0)==1 else 'No'}")
                        if r.get("amount") is not None:
                            try:
                                st.markdown(f"**Valor:** {float(r['amount']):,.0f}")
                            except Exception:
                                st.markdown(f"**Valor:** {r.get('amount')}")
                        st.markdown(f"**Reportó/Creó:** {r.get('created_by_name') or '-'} · **Fecha:** {_short_dt(str(r.get('created_at') or ''))}")
                        st.markdown("**Descripción:**")
                        st.write((r.get("description") or "-").strip())

                        st.markdown("---")

                        # PDF
                        pdf_key = f"_SAN_PDF_{r.get('id')}"
                        cpdf1, cpdf2 = st.columns([1, 2])
                        with cpdf1:
                            if st.button("📄 Preparar PDF", key=f"SAN_pdf_btn_{r.get('id')}"):
                                payload = dict(r)
                                payload["target"] = target
                                pdf_bytes = _build_sanction_pdf(payload)
                                if pdf_bytes is None:
                                    st.warning("Para PDF instala `reportlab`: `pip install reportlab`.")
                                else:
                                    st.session_state[pdf_key] = pdf_bytes
                                    st.success("PDF listo ✅")

                        with cpdf2:
                            pdf_bytes = st.session_state.get(pdf_key)
                            if pdf_bytes:
                                st.download_button(
                                    "⬇️ Descargar PDF",
                                    data=pdf_bytes,
                                    file_name=f"sancion_{r.get('id')}_{now_tz().strftime('%Y%m%d_%H%M')}.pdf",
                                    mime="application/pdf",
                                    key=f"SAN_pdf_dl_{r.get('id')}",
                                )

                        st.markdown("---")

                        # WhatsApp (si scope APARTMENT)
                        if r.get("scope") == "APARTMENT":
                            contact_name, wa = apartment_contact(r, "SANCTION")
                            wa = (wa or "").strip()
                            if wa:
                                default_tpl = (
                                    "Hola {contact_name}, te escribimos desde Administración / Auditoría de Primitiva Parque Natural.\n\n"
                                    "Se registró una *sanción* asociada a {target}:\n"
                                    "• Tipo: {sanction_type}\n"
                                    "• Fecha: {created_at}\n"
                                    "• Bloquea ingreso: {block_entry}\n"
                                    "• Valor: {amount}\n"
                                    "• Detalle: {description}\n\n"
                                    "Nota: si requieres la evidencia fotográfica, la compartimos por este medio.\n"
                                    "Por favor evitar reincidencia. ¡Gracias!"
                                )
                                tpl = get_config(con, "auditor_sanction_whatsapp_template", default_tpl)
                                ctx = {
                                    "contact_name": contact_name or "vecino/a",
                                    "target": target,
                                    "sanction_type": r.get("sanction_type") or "-",
                                    "created_at": _short_dt(str(r.get("created_at") or "")),
                                    "block_entry": "Sí" if int(r.get("block_entry") or 0) == 1 else "No",
                                    "amount": (f"{float(r['amount']):,.0f}" if r.get("amount") is not None else "-"),
                                    "description": (r.get("description") or "-").strip(),
                                }
                                msg = _render_tpl(tpl, ctx)
                                url = _wa_url(wa, msg)
                                if url:
                                    try:
                                        st.link_button("📲 Enviar aviso por WhatsApp", url)
                                    except Exception:
                                        st.markdown(f"[📲 Enviar aviso por WhatsApp]({url})")
                            else:
                                st.info("Este apartamento no tiene WhatsApp registrado (Admin → Estructura → Apartamentos).")

                        st.markdown("---")

                        # Cierre
                        reason = st.text_input("Motivo de cierre", key=f"SAN_close_reason_{r.get('id')}")
                        if st.button("✅ Cerrar sanción", key=f"SAN_close_btn_{r.get('id')}", type="primary"):
                            con.execute(
                                "UPDATE sanctions SET status='CLOSED', closed_at=?, closed_by=?, close_reason=? WHERE id=?",
                                (now_tz().isoformat(), int(u.id), reason.strip() if reason else None, int(r["id"])),
                            )
                            con.commit()
                            audit("SANCTION_CLOSE", u.id, {"id": int(r["id"]), "reason": reason})
                            st.success("Cerrada ✅")
                            st.rerun()

                    with right:
                        ev_path = str(r.get("evidence_path") or "").strip()
                        if ev_path and _safe_path_exists(ev_path):
                            st_image_safe(ev_path, caption="Evidencia")
                            try:
                                with open(ev_path, "rb") as f:
                                    st.download_button(
                                        "⬇️ Descargar evidencia",
                                        data=f.read(),
                                        file_name=os.path.basename(ev_path),
                                        key=f"SAN_ev_dl_{r.get('id')}",
                                    )
                            except Exception:
                                pass
                        else:
                            st.caption("Sin evidencia")

    # -------------------------
    # Historial (cerradas) con detalle + evidencia + PDF
    # -------------------------
    with tab_hist:
        st.markdown("### Historial (cerradas) y reiteración")
        days = st.slider("Ventana de análisis (días)", 7, 365, 90, key="SAN_hist_days")
        since = now_tz() - timedelta(days=int(days))
        qtxt = st.text_input("Buscar (placa / T#-apto / tipo)", key="SAN_hist_q").strip().lower()
        limit = st.slider("Máximo a cargar", 100, 2000, 800, key="SAN_hist_limit")

        closed = con.execute(
            """
            SELECT s.*,
                   u.full_name as created_by_name,
                   u2.full_name as closed_by_name,
                   t.tower_num, a.apt_number,
                   a.resident_name, a.whatsapp,
                   COALESCE(a.is_rented,0) as is_rented,
                   a.owner_name as owner_name, a.owner_whatsapp as owner_whatsapp,
                   a.tenant_name as tenant_name, a.tenant_whatsapp as tenant_whatsapp
            FROM sanctions s
            JOIN users u ON u.id=s.created_by
            LEFT JOIN users u2 ON u2.id=s.closed_by
            LEFT JOIN apartments a ON a.id=s.apt_id
            LEFT JOIN towers t ON t.id=a.tower_id
            WHERE s.status='CLOSED' AND s.created_at >= ?
            ORDER BY s.closed_at DESC
            LIMIT ?
            """,
            (since.isoformat(), int(limit)),
        ).fetchall()
        closed_d = [_rowdict(x) for x in closed]

        if not closed_d:
            st.info("No hay sanciones cerradas en esa ventana.")
        else:
            def _target(rr: Dict[str, Any]) -> str:
                return (
                    f"T{rr.get('tower_num','-')}-{rr.get('apt_number','-')}"
                    if rr.get("scope") == "APARTMENT"
                    else (rr.get("plate") or "-")
                )

            df = pd.DataFrame(
                [
                    {
                        "ID": int(r.get("id")),
                        "Creada": _short_dt(str(r.get("created_at") or "")),
                        "Cerrada": _short_dt(str(r.get("closed_at") or "")),
                        "Tipo": r.get("sanction_type") or "",
                        "Objetivo": _target(r),
                        "Bloquea": "Sí" if int(r.get("block_entry") or 0) == 1 else "No",
                        "Valor": float(r["amount"]) if r.get("amount") is not None else None,
                        "Cerró": r.get("closed_by_name") or "-",
                        "Motivo": r.get("close_reason") or "",
                        "Evidencia": "Sí" if str(r.get("evidence_path") or "").strip() else "No",
                    }
                    for r in closed_d
                ]
            )

            if qtxt:
                df2 = df.copy()
                df2["_q"] = (df2["Tipo"].fillna("") + " " + df2["Objetivo"].fillna("")).str.lower()
                df = df2[df2["_q"].str.contains(qtxt, na=False)].drop(columns=["_q"])

            st_df(df.drop(columns=["ID"], errors="ignore"))

            st.markdown("#### Top reiterativos (por objetivo) · ventana seleccionada")
            if not df.empty:
                top = (
                    df.groupby("Objetivo")
                    .size()
                    .reset_index(name="Sanciones")
                    .sort_values(["Sanciones", "Objetivo"], ascending=[False, True])
                    .head(15)
                )
                st_df(top)

            st.markdown("---")
            st.markdown("### Detalle (cerradas)")

            ids = df["ID"].tolist() if "ID" in df.columns else [int(x.get("id")) for x in closed_d]
            sid = st.selectbox("Sanción", ids, format_func=lambda i: f"#{i}", key="SAN_hist_sel")
            rr = next((x for x in closed_d if int(x.get("id")) == int(sid)), None)

            if rr:
                target = _target(rr)
                left, right = st.columns([2.2, 1.2])

                with left:
                    st.markdown(f"**Tipo:** {rr.get('sanction_type','-')}")
                    st.markdown(f"**Objetivo:** {target}")
                    st.caption(
                        f"Creada: {_short_dt(str(rr.get('created_at') or ''))} · "
                        f"Cerrada: {_short_dt(str(rr.get('closed_at') or ''))} · "
                        f"Cerró: {rr.get('closed_by_name') or '-'}"
                    )
                    st.markdown("**Descripción:**")
                    st.write((rr.get("description") or "-").strip())
                    if (rr.get("close_reason") or "").strip():
                        st.markdown("**Motivo de cierre:**")
                        st.write(str(rr.get("close_reason") or "").strip())

                    st.markdown("---")

                    pdf_key = f"_SANH_PDF_{rr.get('id')}"
                    cpdf1, cpdf2 = st.columns([1, 2])
                    with cpdf1:
                        if st.button("📄 Preparar PDF", key=f"SAN_hist_pdf_btn_{rr.get('id')}"):
                            payload = dict(rr)
                            payload["target"] = target
                            pdf_bytes = _build_sanction_pdf(payload)
                            if pdf_bytes is None:
                                st.warning("Para PDF instala `reportlab`: `pip install reportlab`.")
                            else:
                                st.session_state[pdf_key] = pdf_bytes
                                st.success("PDF listo ✅")

                    with cpdf2:
                        pdf_bytes = st.session_state.get(pdf_key)
                        if pdf_bytes:
                            st.download_button(
                                "⬇️ Descargar PDF",
                                data=pdf_bytes,
                                file_name=f"sancion_{rr.get('id')}_{now_tz().strftime('%Y%m%d_%H%M')}.pdf",
                                mime="application/pdf",
                                key=f"SAN_hist_pdf_dl_{rr.get('id')}",
                            )

                with right:
                    ev_path = str(rr.get("evidence_path") or "").strip()
                    if ev_path and _safe_path_exists(ev_path):
                        st_image_safe(ev_path, caption="Evidencia")
                        try:
                            with open(ev_path, "rb") as f:
                                st.download_button(
                                    "⬇️ Descargar evidencia",
                                    data=f.read(),
                                    file_name=os.path.basename(ev_path),
                                    key=f"SAN_hist_ev_dl_{rr.get('id')}",
                                )
                        except Exception:
                            pass
                    else:
                        st.caption("Sin evidencia")

        st.caption("Tip: aumenta la ventana de días para ver más histórico.")

    con.close()


# ============================================================
# AUDITOR: INCIDENCIAS
# ============================================================

def page_incidents():
    require_role("AUDITOR", "ADMIN")
    con = db_connect()
    st.subheader("🚩 Comparendos")

    open_n = count_open_incidents(con)
    st.info(f"Incidencias abiertas: **{open_n}**")

    thr, win_days = _incident_repeat_cfg(con)
    since = now_tz() - timedelta(days=int(win_days))

    # compatibilidad schema
    inc_has_evidence = _table_has_column(con, "incidents", "evidence_path")
    inc_has_close_notes = _table_has_column(con, "incidents", "close_notes")

    apt_has_is_rented = _table_has_column(con, "apartments", "is_rented")
    apt_has_owner_name = _table_has_column(con, "apartments", "owner_name")
    apt_has_owner_wa = _table_has_column(con, "apartments", "owner_whatsapp")
    apt_has_tenant_name = _table_has_column(con, "apartments", "tenant_name")
    apt_has_tenant_wa = _table_has_column(con, "apartments", "tenant_whatsapp")

    tab_open, tab_hist = st.tabs(["🟥 Pendientes", "📚 Historial y estadísticas"])

    # =========================
    # Pendientes
    # =========================
    with tab_open:
        st.caption(
            f"Regla de reiteración: si un apartamento acumula **{thr}+** incidencias en **{win_days}** días, "
            "se marcará como **reiterativo** y sugerirá crear sanción."
        )

        f_plate = st.text_input("Filtrar por placa (opcional)", key="INC_f_plate").strip().upper()
        f_stage = st.multiselect("Filtrar por etapa (opcional)", options=[1, 2, 3, 4, 5, 6], default=[], key="INC_f_stage")

        # conteo por apto en ventana
        cnt_rows = con.execute(
            """
            SELECT apt_id, COUNT(*) as n
            FROM incidents
            WHERE created_at >= ?
            GROUP BY apt_id
            """,
            (since.isoformat(),),
        ).fetchall()
        cnt_map = {int(r["apt_id"]): int(r["n"]) for r in cnt_rows}

        # sanciones ya creadas desde incidencias (1 query)
        sanc_map: Dict[int, Dict[str, Any]] = {}
        try:
            sanc_rows = con.execute(
                """
                SELECT id, status, created_at, description
                FROM sanctions
                WHERE description LIKE '[Incidencia #%]%'
                ORDER BY id DESC
                LIMIT 5000
                """
            ).fetchall()
            for s in sanc_rows:
                desc = str(s["description"] or "")
                try:
                    p1 = desc.find("#")
                    p2 = desc.find("]", p1 + 1)
                    inc_id = int(desc[p1 + 1 : p2])
                    sanc_map[inc_id] = {"id": int(s["id"]), "status": s["status"], "created_at": s["created_at"]}
                except Exception:
                    continue
        except Exception:
            sanc_map = {}

        sel_evidence = "i.evidence_path as evidence_path" if inc_has_evidence else "'' as evidence_path"
        sel_close_notes = "i.close_notes as close_notes" if inc_has_close_notes else "'' as close_notes"

        sel_is_rented = "COALESCE(a.is_rented,0) as is_rented" if apt_has_is_rented else "0 as is_rented"
        sel_owner_name = "a.owner_name as owner_name" if apt_has_owner_name else "'' as owner_name"
        sel_owner_wa = "a.owner_whatsapp as owner_whatsapp" if apt_has_owner_wa else "'' as owner_whatsapp"
        sel_tenant_name = "a.tenant_name as tenant_name" if apt_has_tenant_name else "'' as tenant_name"
        sel_tenant_wa = "a.tenant_whatsapp as tenant_whatsapp" if apt_has_tenant_wa else "'' as tenant_whatsapp"

        q = f"""
            SELECT i.*,
                   {sel_evidence},
                   {sel_close_notes},
                   t.tower_num, t.etapa_residencial, a.apt_number,
                   a.resident_name as resident_name, a.whatsapp as whatsapp,
                   {sel_is_rented},
                   {sel_owner_name}, {sel_owner_wa},
                   {sel_tenant_name}, {sel_tenant_wa},
                   z.name as zone_name, pc.code as cell_code,
                   u.full_name as created_by_name
            FROM incidents i
            JOIN apartments a ON a.id=i.apt_id
            JOIN towers t ON t.id=a.tower_id
            LEFT JOIN zones z ON z.id=i.zone_id
            LEFT JOIN private_cells pc ON pc.id=i.private_cell_id
            LEFT JOIN users u ON u.id=i.created_by
            WHERE i.status='OPEN'
        """
        params: List[Any] = []
        if f_plate:
            q += " AND i.plate LIKE ?"
            params.append(f"%{normalize_plate(f_plate)}%")
        if f_stage:
            q += " AND t.etapa_residencial IN ({})".format(",".join(["?"] * len(f_stage)))
            params.extend([int(x) for x in f_stage])

        q += " ORDER BY i.created_at DESC"
        rows = con.execute(q, tuple(params)).fetchall()
        rows = [_rowdict(r) for r in rows]

        if not rows:
            st.success("No hay incidencias abiertas con esos filtros ✅")
        else:
            df = pd.DataFrame(
                [
                    {
                        "Fecha": _short_dt(str(r.get("created_at") or "")),
                        "Placa": r.get("plate", ""),
                        "Tipo": r.get("vehicle_type", ""),
                        "Perfil": r.get("ticket_type", ""),
                        "Etapa": int(r.get("etapa_residencial") or 0),
                        "Apto": f"T{r.get('tower_num','-')}-{r.get('apt_number','-')}",
                        "Lugar": (r.get("cell_code") or (r.get("zone_name") or "-")),
                        "Evidencia": "Sí" if str(r.get("evidence_path") or "").strip() else "No",
                        "Reportó": r.get("created_by_name") or "-",
                        "Reiteración": int(cnt_map.get(int(r.get("apt_id") or 0), 0)),
                        "Detalle": r.get("description") or "",
                    }
                    for r in rows
                ]
            )
            st_df(df)

            st.markdown("### Gestionar incidencias")
            u = current_user()
            assert u is not None

            max_cards = st.slider("Mostrar fichas (máximo)", 5, 80, 20, key="INC_open_cards_max")

            for r in rows[: int(max_cards)]:
                place = r.get("cell_code") or (r.get("zone_name") or "-")
                apt_key = f"T{r.get('tower_num','-')}-{r.get('apt_number','-')}"
                n_rep = int(cnt_map.get(int(r.get("apt_id") or 0), 0))
                flag = "🔴" if n_rep >= thr else ("🟠" if n_rep == max(thr - 1, 1) else "🟢")

                title = f"{flag} #{r.get('id')} · {r.get('plate')} · {apt_key} · {_short_dt(str(r.get('created_at') or ''))}"
                with st.expander(title, expanded=False):
                    if n_rep >= thr:
                        st.warning(
                            f"Reiterativo: **{n_rep}** incidencias en los últimos **{win_days}** días. "
                            "Se recomienda crear sanción."
                        )
                    else:
                        st.caption(f"Incidencias en ventana: **{n_rep}** / umbral **{thr}**")

                    left, right = st.columns([2.2, 1.2])

                    with left:
                        st.markdown(f"**Placa:** `{r.get('plate','-')}` · **Tipo:** {r.get('vehicle_type','-')} · **Perfil:** {r.get('ticket_type','-')}")
                        st.markdown(f"**Destino:** {apt_key} · **Etapa:** {r.get('etapa_residencial','-')}")
                        st.markdown(f"**Lugar:** {place}")
                        st.markdown(f"**Reportó:** {r.get('created_by_name','-')} · **Fecha:** {_short_dt(str(r.get('created_at') or ''))}")
                        st.markdown("**Detalle:**")
                        st.write((r.get("description") or "-").strip())

                    ev_path = str(r.get("evidence_path") or "").strip()
                    with right:
                        if ev_path and _safe_path_exists(ev_path):
                            st_image_safe(ev_path, caption="Evidencia")
                            try:
                                with open(ev_path, "rb") as f:
                                    st.download_button(
                                        "⬇️ Descargar evidencia",
                                        data=f.read(),
                                        file_name=os.path.basename(ev_path),
                                        key=f"INC_ev_dl_{r.get('id')}",
                                    )
                            except Exception:
                                pass
                        else:
                            st.caption("Sin evidencia")

                    st.markdown("---")

                    # PDF
                    pdf_key = f"_INC_PDF_{r.get('id')}"
                    cpdf1, cpdf2 = st.columns([1, 2])
                    with cpdf1:
                        if st.button("📄 Preparar PDF", key=f"INC_pdf_btn_{r.get('id')}"):
                            inc_ctx = dict(r)
                            inc_ctx["place"] = place
                            pdf_bytes = _build_incident_pdf(inc_ctx)
                            if pdf_bytes is None:
                                st.warning("Para PDF instala `reportlab`: `pip install reportlab`.")
                            else:
                                st.session_state[pdf_key] = pdf_bytes
                                st.success("PDF listo ✅")

                    with cpdf2:
                        pdf_bytes = st.session_state.get(pdf_key)
                        if pdf_bytes:
                            st.download_button(
                                "⬇️ Descargar PDF",
                                data=pdf_bytes,
                                file_name=f"incidencia_{r.get('id')}_{r.get('plate','')}.pdf",
                                mime="application/pdf",
                                key=f"INC_pdf_dl_{r.get('id')}",
                            )

                    st.markdown("---")

                    # WhatsApp (según arrendado/propietario)
                    contact_name, whatsapp = apartment_contact(r, "SANCTION")
                    whatsapp = (whatsapp or "").strip()
                    if whatsapp:
                        tipo_txt = "moto" if (r.get("vehicle_type") == "MOTO") else "carro"
                        default_tpl = (
                            "Hola {resident_name}, te escribimos desde Administración / Auditoría de Primitiva Parque Natural.\n\n"
                            "Se registró una *incidencia* asociada a tu apartamento Torre {tower_num}, Apto {apt_number}:\n"
                            "• Placa: {plate}\n"
                            "• Vehículo: {vehicle_type_text} ({vehicle_type})\n"
                            "• Lugar/Zona: {place}\n"
                            "• Fecha del reporte: {created_at}\n"
                            "• Detalle: {description}\n\n"
                            "Este mensaje es un *preaviso* (llamado de atención). Si este comportamiento se repite, podría convertirse en una *sanción*.\n\n"
                            "Por favor ayúdanos confirmando si ya se resolvió y evitando que vuelva a ocurrir.\n"
                            "¡Gracias!"
                        )
                        tpl = get_config(con, "auditor_incident_whatsapp_template", default_tpl)
                        ctx = {
                            "resident_name": (contact_name or "vecino/a"),
                            "tower_num": r.get("tower_num"),
                            "apt_number": r.get("apt_number"),
                            "plate": r.get("plate"),
                            "vehicle_type": r.get("vehicle_type"),
                            "vehicle_type_text": tipo_txt,
                            "place": place,
                            "created_at": _short_dt(str(r.get("created_at") or "")),
                            "description": (r.get("description") or "-").strip(),
                        }
                        msg = _render_tpl(tpl, ctx)
                        url = _wa_url(whatsapp, msg)
                        if url:
                            try:
                                st.link_button("📲 Enviar preaviso por WhatsApp", url)
                            except Exception:
                                st.markdown(f"[📲 Enviar preaviso por WhatsApp]({url})")
                    else:
                        st.info("Este apartamento no tiene WhatsApp registrado (Admin → Estructura → Apartamentos).")

                    st.markdown("---")

                    # Acciones
                    close_notes = st.text_input("Notas de cierre (obligatorio para cerrar)", key=f"INC_close_{r.get('id')}")
                    c1, c2 = st.columns(2)

                    with c1:
                        if st.button("✅ Cerrar incidencia", key=f"INC_close_btn_{r.get('id')}", type="primary"):
                            if not close_notes.strip():
                                st.error("Debes escribir una justificación / notas de cierre.")
                            else:
                                con.execute(
                                    """
                                    UPDATE incidents
                                    SET status='CLOSED', closed_at=?, closed_by=?, close_notes=?
                                    WHERE id=?
                                    """,
                                    (now_tz().isoformat(), int(u.id), close_notes.strip(), int(r.get("id"))),
                                )
                                con.commit()
                                audit("INCIDENT_CLOSE", u.id, {"incident_id": int(r.get("id")), "plate": r.get("plate")})
                                st.success("Incidencia cerrada ✅")
                                st.rerun()

                    with c2:
                        inc_id = int(r.get("id") or 0)
                        existing = sanc_map.get(inc_id)
                        prefix = f"[Incidencia #{inc_id}]"

                        if existing:
                            st.success(f"✅ Ya existe sanción desde esta incidencia: #{existing['id']} · {existing['status']} · {_short_dt(existing['created_at'])}")
                            st.button("🧾 Crear sanción desde incidencia", key=f"INC_toSAN_btn_{inc_id}", disabled=True)
                        else:
                            confirm = st.checkbox(
                                "Confirmo crear una sanción por esta incidencia (quedará ACTIVA).",
                                value=(n_rep >= thr),
                                key=f"INC_toSAN_confirm_{inc_id}",
                            )
                            if st.button("🧾 Crear sanción desde incidencia", key=f"INC_toSAN_btn_{inc_id}", disabled=not confirm):
                                desc = f"{prefix} {r.get('description') or ''}".strip()

                                # ✅ Copiamos evidencia si existe (para que la sanción se vea igual y con foto)
                                ev_to_copy = ev_path if (ev_path and _safe_path_exists(ev_path)) else None

                                con.execute(
                                    """
                                    INSERT INTO sanctions(scope, apt_id, plate, sanction_type, description, evidence_path,
                                                          amount, block_entry, status, created_at, created_by)
                                    VALUES (?,?,?,?,?,?,?,?,?,?,?)
                                    """,
                                    (
                                        "PLATE",
                                        None,
                                        r.get("plate"),
                                        "Incidencia reportada",
                                        desc,
                                        ev_to_copy,
                                        None,
                                        1,
                                        "ACTIVE",
                                        now_tz().isoformat(),
                                        int(u.id),
                                    ),
                                )
                                con.commit()
                                audit("SANCTION_CREATE_FROM_INCIDENT", u.id, {"incident_id": inc_id, "plate": r.get("plate")})
                                st.success("Sanción creada ✅ (revisa en Auditor · Sanciones)")
                                st.rerun()

    # =========================
    # Historial (cerradas)
    # =========================
    with tab_hist:
        st.markdown("### Historial de incidencias (cerradas)")
        days = st.slider("Ventana (días)", 7, 365, int(win_days), key="INC_hist_days")
        since2 = now_tz() - timedelta(days=int(days))
        qtxt = st.text_input("Buscar (placa / T#-apto)", key="INC_hist_q").strip().upper()
        limit = st.slider("Máximo a cargar", 100, 5000, 1200, key="INC_hist_limit")

        sel_evidence = "i.evidence_path as evidence_path" if inc_has_evidence else "'' as evidence_path"
        sel_close_notes = "i.close_notes as close_notes" if inc_has_close_notes else "'' as close_notes"

        hist = con.execute(
            f"""
            SELECT i.*,
                   {sel_evidence},
                   {sel_close_notes},
                   t.tower_num, t.etapa_residencial, a.apt_number,
                   u.full_name as created_by_name,
                   u2.full_name as closed_by_name,
                   z.name as zone_name, pc.code as cell_code
            FROM incidents i
            JOIN apartments a ON a.id=i.apt_id
            JOIN towers t ON t.id=a.tower_id
            LEFT JOIN users u ON u.id=i.created_by
            LEFT JOIN users u2 ON u2.id=i.closed_by
            LEFT JOIN zones z ON z.id=i.zone_id
            LEFT JOIN private_cells pc ON pc.id=i.private_cell_id
            WHERE i.status='CLOSED' AND i.created_at >= ?
            ORDER BY i.closed_at DESC
            LIMIT ?
            """,
            (since2.isoformat(), int(limit)),
        ).fetchall()
        hist = [_rowdict(r) for r in hist]

        if not hist:
            st.info("No hay incidencias cerradas en esa ventana.")
        else:
            dfh = pd.DataFrame(
                [
                    {
                        "ID": int(r.get("id")),
                        "Creada": _short_dt(str(r.get("created_at") or "")),
                        "Cerrada": _short_dt(str(r.get("closed_at") or "")),
                        "Placa": r.get("plate"),
                        "Tipo": r.get("vehicle_type"),
                        "Perfil": r.get("ticket_type"),
                        "Etapa": int(r.get("etapa_residencial") or 0),
                        "Apto": f"T{r.get('tower_num','-')}-{r.get('apt_number','-')}",
                        "Lugar": (r.get("cell_code") or (r.get("zone_name") or "-")),
                        "Evidencia": "Sí" if str(r.get("evidence_path") or "").strip() else "No",
                        "Notas cierre": r.get("close_notes") or "",
                        "Detalle": r.get("description") or "",
                    }
                    for r in hist
                ]
            )

            if qtxt:
                ql = qtxt.strip().upper()
                dfh2 = dfh.copy()
                dfh2["_q"] = (dfh2["Placa"].fillna("") + " " + dfh2["Apto"].fillna("")).str.upper()
                dfh = dfh2[dfh2["_q"].str.contains(ql, na=False)].drop(columns=["_q"])

            st_df(dfh.drop(columns=["ID"], errors="ignore"))

            st.markdown("#### Top reiterativos (por apartamento) · ventana seleccionada")
            top = (
                dfh.groupby("Apto")
                .size()
                .reset_index(name="Incidencias")
                .sort_values(["Incidencias", "Apto"], ascending=[False, True])
                .head(15)
            )
            st_df(top)

            st.markdown("---")
            st.markdown("### Detalle (cerradas)")

            ids = dfh["ID"].tolist() if "ID" in dfh.columns else [int(x.get("id")) for x in hist]
            iid = st.selectbox("Incidencia", ids, format_func=lambda i: f"#{i}", key="INC_hist_sel")
            rr = next((x for x in hist if int(x.get("id")) == int(iid)), None)

            if rr:
                place = (rr.get("cell_code") or (rr.get("zone_name") or "-"))
                apt_key = f"T{rr.get('tower_num','-')}-{rr.get('apt_number','-')}"
                left, right = st.columns([2.2, 1.2])

                with left:
                    st.markdown(f"**Estado:** CLOSED")
                    st.markdown(f"**Placa:** `{rr.get('plate','-')}` · **Tipo:** {rr.get('vehicle_type','-')} · **Perfil:** {rr.get('ticket_type','-')}")
                    st.markdown(f"**Destino:** {apt_key} · **Etapa:** {rr.get('etapa_residencial','-')}")
                    st.markdown(f"**Lugar:** {place}")
                    st.caption(
                        f"Creada: {_short_dt(str(rr.get('created_at') or ''))} · "
                        f"Cerrada: {_short_dt(str(rr.get('closed_at') or ''))} · "
                        f"Cerró: {rr.get('closed_by_name') or '-'}"
                    )
                    st.markdown("**Detalle:**")
                    st.write((rr.get("description") or "-").strip())
                    if (rr.get("close_notes") or "").strip():
                        st.markdown("**Notas cierre:**")
                        st.write(str(rr.get("close_notes") or "").strip())

                    st.markdown("---")

                    pdf_key = f"_INC_HPDF_{rr.get('id')}"
                    cpdf1, cpdf2 = st.columns([1, 2])
                    with cpdf1:
                        if st.button("📄 Preparar PDF", key=f"INC_hist_pdf_btn_{rr.get('id')}"):
                            inc_ctx = dict(rr)
                            inc_ctx["place"] = place
                            pdf_bytes = _build_incident_pdf(inc_ctx)
                            if pdf_bytes is None:
                                st.warning("Para PDF instala `reportlab`: `pip install reportlab`.")
                            else:
                                st.session_state[pdf_key] = pdf_bytes
                                st.success("PDF listo ✅")

                    with cpdf2:
                        pdf_bytes = st.session_state.get(pdf_key)
                        if pdf_bytes:
                            st.download_button(
                                "⬇️ Descargar PDF",
                                data=pdf_bytes,
                                file_name=f"incidencia_{rr.get('id')}_{rr.get('plate','')}.pdf",
                                mime="application/pdf",
                                key=f"INC_hist_pdf_dl_{rr.get('id')}",
                            )

                with right:
                    ev_path = str(rr.get("evidence_path") or "").strip()
                    if ev_path and _safe_path_exists(ev_path):
                        st_image_safe(ev_path, caption="Evidencia")
                        try:
                            with open(ev_path, "rb") as f:
                                st.download_button(
                                    "⬇️ Descargar evidencia",
                                    data=f.read(),
                                    file_name=os.path.basename(ev_path),
                                    key=f"INC_hist_ev_dl_{rr.get('id')}",
                                )
                        except Exception:
                            pass
                    else:
                        st.caption("Sin evidencia")

    con.close()


# ============================================================
# AUDITOR: SEGUIMIENTO (uso del sistema)
# ============================================================

def page_guard_audit():
    """
    🕵️ Seguimiento (Uso del sistema)
    - Enfoque: productividad / uso / tiempos de atención (NO culpa).
    - Parte Auditoría con tiempos: visible SOLO para ADMIN.
    """
    require_role("AUDITOR", "ADMIN")
    con = db_connect()
    u = current_user()
    assert u is not None

    st.subheader("🕵️ Seguimiento (uso del sistema)")
    st.caption("Mide acciones realizadas y tiempos de gestión. No evalúa 'faltas' del usuario.")

    days = st.slider("Días hacia atrás", 1, 30, 7, key="GA_days")
    since = now_tz() - timedelta(days=days)
    since_iso = since.isoformat()
    st.caption(f"Mostrando información desde: **{since.strftime('%Y-%m-%dT%H:%M')}**")

    if u.role == "ADMIN":
        tab_ops, tab_aud, tab_evt = st.tabs(["🚪 Operación (Portería / Uso)", "🧾 Auditoría (solo Admin)", "🧷 Eventos (opcional)"])
    else:
        (tab_ops,) = st.tabs(["🚪 Operación (Portería / Uso)"])
        tab_aud = None
        tab_evt = None

    # -------------------------
    # TAB Operación
    # -------------------------
    with tab_ops:
        rows = con.execute(
            """
            SELECT al.event_time, al.action, al.details_json,
                   u.username, u.full_name, u.role
            FROM audit_log al
            LEFT JOIN users u ON u.id=al.user_id
            WHERE al.event_time >= ?
            ORDER BY al.event_time DESC
            """,
            (since_iso,),
        ).fetchall()

        if not rows:
            st.info("Sin auditoría en ese rango.")
        else:
            def _count(action: str) -> int:
                return sum(1 for rr in rows if (rr["action"] == action))

            total_events = len(rows)
            k_gate_in = _count("GATE_IN")
            k_gate_out = _count("GATE_OUT")
            k_end_confirm = _count("END_DAY_CONFIRM_EXIT")
            k_end_incid = _count("END_DAY_INCIDENT") + _count("INCIDENT_CREATE")
            k_inc_close = _count("INCIDENT_CLOSE")
            k_san_create = _count("SANCTION_CREATE") + _count("SANCTION_CREATE_FROM_INCIDENT")
            k_san_close = _count("SANCTION_CLOSE")

            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Eventos", int(total_events))
            c2.metric("Ingresos registrados", int(k_gate_in))
            c3.metric("Salidas registradas", int(k_gate_out))
            c4.metric("Cierre día (confirm)", int(k_end_confirm))

            c5, c6, c7, c8 = st.columns(4)
            c5.metric("Cierre día (incid reportadas)", int(k_end_incid))
            c6.metric("Incidencias cerradas", int(k_inc_close))
            c7.metric("Sanciones creadas", int(k_san_create))
            c8.metric("Sanciones cerradas", int(k_san_close))

            st.markdown("---")

            agg: Dict[Tuple[str, str, str], Dict[str, int]] = {}
            for rr in rows:
                uname = (rr["username"] or "N/A").strip()
                role = (rr["role"] or "N/A").strip()
                fname = (rr["full_name"] or uname).strip()
                key = (uname, role, fname)

                if key not in agg:
                    agg[key] = {
                        "GATE_IN": 0,
                        "GATE_OUT": 0,
                        "END_DAY_CONFIRM_EXIT": 0,
                        "END_DAY_INCIDENT": 0,
                        "INCIDENT_CLOSE": 0,
                        "SANCTION_CREATE": 0,
                        "SANCTION_CLOSE": 0,
                        "EVENTS": 0,
                    }

                agg[key]["EVENTS"] += 1
                act = rr["action"]

                if act in ("SANCTION_CREATE", "SANCTION_CREATE_FROM_INCIDENT"):
                    agg[key]["SANCTION_CREATE"] += 1
                elif act in agg[key]:
                    agg[key][act] += 1

            df_users = pd.DataFrame(
                [
                    {
                        "Usuario": k[0],
                        "Rol": k[1],
                        "Nombre": k[2],
                        "Eventos": int(v["EVENTS"]),
                        "Ingresos": int(v["GATE_IN"]),
                        "Salidas": int(v["GATE_OUT"]),
                        "Cierre día (confirm)": int(v["END_DAY_CONFIRM_EXIT"]),
                        "Cierre día (incid reportadas)": int(v["END_DAY_INCIDENT"]),
                        "Incidencias (cerró)": int(v["INCIDENT_CLOSE"]),
                        "Sanciones (creó)": int(v["SANCTION_CREATE"]),
                        "Sanciones (cerró)": int(v["SANCTION_CLOSE"]),
                    }
                    for k, v in agg.items()
                ]
            )

            if not df_users.empty:
                df_users["Operaciones (portería)"] = (
                    df_users["Ingresos"]
                    + df_users["Salidas"]
                    + df_users["Cierre día (confirm)"]
                    + df_users["Cierre día (incid reportadas)"]
                ).astype(int)

                def _balance(row):
                    ins = int(row["Ingresos"])
                    outs = int(row["Salidas"])
                    if ins <= 0:
                        return 0.0 if outs <= 0 else 100.0
                    return round((outs / ins) * 100.0, 1)

                df_users["Balance Salidas/Ingresos (%)"] = df_users.apply(_balance, axis=1)

            st.markdown("### Ranking (vista rápida)")
            with st.expander("Filtros (opcional)", expanded=False):
                roles = sorted({str(x["Rol"]) for _, x in df_users.iterrows()}) if not df_users.empty else []
                role_f = st.multiselect("Rol", roles, default=roles, key="GA_role_f")
                q_user = st.text_input("Buscar usuario/nombre", key="GA_user_q").strip().lower()
                sort_by = st.selectbox(
                    "Ordenar por",
                    [
                        "Operaciones (portería)",
                        "Ingresos",
                        "Salidas",
                        "Balance Salidas/Ingresos (%)",
                        "Cierre día (confirm)",
                        "Cierre día (incid reportadas)",
                    ],
                    index=0,
                    key="GA_sort_by",
                )

            df_view = df_users.copy()
            if not df_view.empty:
                if role_f:
                    df_view = df_view[df_view["Rol"].isin(role_f)]
                if q_user:
                    df_view = df_view[
                        df_view["Usuario"].str.lower().str.contains(q_user)
                        | df_view["Nombre"].str.lower().str.contains(q_user)
                    ]
                df_view = df_view.sort_values([sort_by, "Rol", "Usuario"], ascending=[False, True, True])

            if df_view.empty:
                st.info("No hay resultados con esos filtros.")
            else:
                st_df(df_view)

    # -------------------------
    # TAB Auditoría (solo ADMIN)
    # -------------------------
    if tab_aud is not None:
        with tab_aud:
            st.markdown("### ⏱️ Tiempos de gestión de incidencias (Auditores)")
            st.caption("Se calcula: created_at → closed_at (cuando se cierra una incidencia).")

            sla_hours = float(get_config(con, "auditor_incident_sla_hours", 6))
            sla_min = int(sla_hours * 60)
            st.info(f"SLA referencia: **{sla_hours:.0f} h**")

            inc_closed = con.execute(
                """
                SELECT i.id, i.created_at, i.closed_at, i.closed_by,
                       u.username, u.full_name, u.role
                FROM incidents i
                LEFT JOIN users u ON u.id=i.closed_by
                WHERE i.status='CLOSED'
                  AND i.closed_at IS NOT NULL
                  AND i.closed_at >= ?
                ORDER BY i.closed_at DESC
                """,
                (since_iso,),
            ).fetchall()

            if not inc_closed:
                st.info("No hay incidencias cerradas en este rango.")
            else:
                df_c = pd.DataFrame([_rowdict(r) for r in inc_closed])
                df_c["created_at_ts"] = pd.to_datetime(df_c["created_at"], errors="coerce")
                df_c["closed_at_ts"] = pd.to_datetime(df_c["closed_at"], errors="coerce")
                df_c["dur_min"] = ((df_c["closed_at_ts"] - df_c["created_at_ts"]).dt.total_seconds() // 60).fillna(0).astype(int)

                perf = []
                for uid, g in df_c.groupby("closed_by"):
                    who = g.iloc[0]
                    uname = who.get("username") or "N/A"
                    fname = who.get("full_name") or uname
                    role = who.get("role") or "N/A"

                    s = g["dur_min"]
                    n = int(len(g))
                    avg = float(s.mean())
                    med = float(s.median())
                    p90 = float(s.quantile(0.90))
                    sla_ok = int((s <= sla_min).sum())
                    sla_pct = round((sla_ok / n) * 100.0, 1) if n else 0.0

                    perf.append(
                        {
                            "Auditor": fname,
                            "Usuario": uname,
                            "Rol": role,
                            "Incidencias cerradas": n,
                            "Promedio (min)": int(round(avg)),
                            "Mediana (min)": int(round(med)),
                            "P90 (min)": int(round(p90)),
                            f"% dentro SLA ({sla_hours:.0f}h)": sla_pct,
                        }
                    )
                st_df(pd.DataFrame(perf).sort_values(["Rol", "Usuario"]))

    # -------------------------
    # TAB Eventos (solo ADMIN)
    # -------------------------
    if tab_evt is not None:
        with tab_evt:
            rows = con.execute(
                """
                SELECT al.event_time, al.action, al.details_json,
                    u.username, u.full_name, u.role
                FROM audit_log al
                LEFT JOIN users u ON u.id=al.user_id
                WHERE al.event_time >= ?
                ORDER BY al.event_time DESC
                """,
                (since_iso,),
            ).fetchall()

            if not rows:
                st.info("Sin eventos en ese rango.")
            else:
                users_list = sorted({(r["username"] or "N/A") for r in rows})
                actions_list = sorted({(r["action"] or "N/A") for r in rows})

                f_user = st.selectbox("Usuario", ["Todos"] + users_list, key="GA_evt_user")
                f_action = st.selectbox("Acción", ["Todas"] + actions_list, key="GA_evt_action")
                txt = st.text_input("Buscar en detalle (opcional)", key="GA_evt_txt").strip().lower()
                last_n = st.number_input("Últimos N", 20, 800, 120, key="GA_lastn")

                rows2 = rows[: int(last_n)]
                if f_user != "Todos":
                    rows2 = [r for r in rows2 if (r["username"] or "N/A") == f_user]
                if f_action != "Todas":
                    rows2 = [r for r in rows2 if (r["action"] or "N/A") == f_action]
                if txt:
                    rows2 = [r for r in rows2 if txt in (str(r["details_json"] or "")).lower()]

                df2 = pd.DataFrame(
                    [
                        {
                            "Fecha/Hora": _short_dt(str(r["event_time"])),
                            "Usuario": (r["full_name"] or r["username"] or "-"),
                            "Rol": r["role"] or "-",
                            "Acción": r["action"],
                            "Detalle": r["details_json"],
                        }
                        for r in rows2
                    ]
                )
                st_df(df2)

    con.close()
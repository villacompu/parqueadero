from __future__ import annotations

from datetime import datetime

import pandas as pd
import streamlit as st

from db.core import db_connect, now_tz
from services.config import get_config
from services.parking import normalize_plate, plate_is_valid
from services.vehicle_portal import (
    verify_vehicle_portal,
    sweep_expired_requests,
    create_request,
    get_pending_request,
)


def _mins_left(expires_at: str) -> int:
    ts = pd.to_datetime(expires_at, errors="coerce")
    if pd.isna(ts):
        return 0
    now = pd.Timestamp(now_tz())
    mins = int((ts - now).total_seconds() // 60)
    return max(mins, 0)


def page_resident_portal():
    """Portal para residentes: solicitar ingreso/salida por placa+contraseña."""
    con = db_connect()

    # Limpia expiradas
    try:
        n = sweep_expired_requests(con)
        if n:
            con.commit()
    except Exception:
        pass

    enabled = int(get_config(con, "resident_portal_enabled", 1)) == 1
    ttl = int(get_config(con, "resident_request_ttl_min", 5))
    ttl = max(1, min(ttl, 60))

    entry_enabled = int(get_config(con, "resident_request_entry_enabled", 1)) == 1
    exit_enabled = int(get_config(con, "resident_request_exit_enabled", 1)) == 1

    st.subheader("📲 Portal residentes")
    st.caption(
        "Solicita ingreso/salida para que el portero solo tenga que **aceptar** en pantalla. "
        f"La solicitud es válida por **{ttl} minuto(s)** (configurable)."
    )

    if not enabled:
        st.warning("El portal está desactivado por administración.")
        con.close()
        return

    # -------------------------
    # Sesión portal
    # -------------------------
    plate_session = st.session_state.get("portal_plate")

    if not plate_session:
        with st.form("portal_login", clear_on_submit=False):
            p_in = st.text_input("Placa", placeholder="ABC123")
            pwd = st.text_input("Contraseña", type="password")
            ok = st.form_submit_button("Ingresar")

        if ok:
            plate = normalize_plate(p_in)
            if not plate_is_valid(plate):
                st.error("Placa inválida.")
            else:
                sess = verify_vehicle_portal(con, plate, pwd)
                if not sess:
                    st.error("Credenciales inválidas o vehículo inactivo.")
                else:
                    st.session_state["portal_plate"] = sess.plate
                    st.success("Listo ✅")
                    st.rerun()

        st.info("Si no tienes contraseña, pídela a administración.")
        con.close()
        return

    plate = str(plate_session)

    # Contexto del vehículo
    ctx = con.execute(
        """
        SELECT rv.plate, rv.vehicle_type, a.id as apt_id, a.apt_number, a.resident_name,
               t.tower_num, t.etapa_residencial
        FROM resident_vehicles rv
        JOIN apartments a ON a.id=rv.apt_id
        JOIN towers t ON t.id=a.tower_id
        WHERE rv.plate=? AND rv.is_active=1 AND a.is_active=1 AND t.is_active=1
        """,
        (plate,),
    ).fetchone()

    if not ctx:
        st.error("El vehículo ya no está activo o no se encuentra asociado a un apartamento activo.")
        if st.button("Cerrar sesión", key="portal_logout_err"):
            st.session_state.pop("portal_plate", None)
            st.rerun()
        con.close()
        return

    top1, top2 = st.columns([3, 1])
    with top1:
        who = (ctx["resident_name"] or "").strip() or "Residente"
        st.success(
            f"Conectado: **{plate}** · {('Carro' if ctx['vehicle_type']=='CAR' else 'Moto')} · "
            f"T{ctx['tower_num']}-{ctx['apt_number']} (Etapa {ctx['etapa_residencial']}) · {who}"
        )
    with top2:
        if st.button("Cerrar sesión", key="portal_logout"):
            st.session_state.pop("portal_plate", None)
            st.rerun()

    # Estado actual (¿está dentro?)
    active_tk = con.execute(
        """
        SELECT id, entry_time, ticket_type, zone_id, private_cell_id
        FROM tickets
        WHERE plate=? AND exit_time IS NULL
        ORDER BY entry_time DESC
        LIMIT 1
        """,
        (plate,),
    ).fetchone()

    if active_tk:
        st.info(f"Estado: **DENTRO** · Ingreso: {_fmt(active_tk['entry_time'])}")
    else:
        st.info("Estado: **FUERA**")

    st.markdown("---")

    # Solicitudes pendientes
    pend_entry = get_pending_request(con, plate, "ENTRY")
    pend_exit = get_pending_request(con, plate, "EXIT")

    if pend_entry or pend_exit:
        st.markdown("### ⏳ Solicitudes pendientes")
        if pend_entry:
            st.write(
                f"🟦 **Ingreso** pendiente · vence en **{_mins_left(pend_entry['expires_at'])} min** · "
                f"solicitada: {_fmt(pend_entry['requested_at'])}"
            )
            if st.button("Cancelar solicitud de ingreso", key="portal_cancel_entry"):
                con.execute(
                    "UPDATE resident_requests SET status='CANCELLED' WHERE id=?",
                    (int(pend_entry["id"]),),
                )
                con.commit()
                st.success("Cancelada ✅")
                st.rerun()

        if pend_exit:
            st.write(
                f"🟨 **Salida** pendiente · vence en **{_mins_left(pend_exit['expires_at'])} min** · "
                f"solicitada: {_fmt(pend_exit['requested_at'])}"
            )
            if st.button("Cancelar solicitud de salida", key="portal_cancel_exit"):
                con.execute(
                    "UPDATE resident_requests SET status='CANCELLED' WHERE id=?",
                    (int(pend_exit["id"]),),
                )
                con.commit()
                st.success("Cancelada ✅")
                st.rerun()

        st.markdown("---")

    # Acciones
    st.markdown("### 📌 Solicitar")

    colA, colB = st.columns(2)

    with colA:
        st.markdown("**Ingreso (válido por pocos minutos)**")
        if not entry_enabled:
            st.caption("Deshabilitado por administración")
        elif active_tk:
            st.caption("No disponible: el vehículo ya está marcado como DENTRO.")
        elif pend_entry:
            st.caption("Ya tienes una solicitud de ingreso pendiente.")
        else:
            if st.button("🟦 Solicitar ingreso", key="portal_req_entry", type="primary"):
                try:
                    _ = create_request(con, plate=plate, request_type="ENTRY", ttl_minutes=ttl)
                    con.commit()
                    st.success("Solicitud de ingreso creada ✅")
                    st.rerun()
                except Exception as e:
                    st.error(str(e))

    with colB:
        st.markdown("**Salida (portero debe confirmar)**")
        if not exit_enabled:
            st.caption("Deshabilitado por administración")
        elif not active_tk:
            st.caption("No disponible: el vehículo está marcado como FUERA.")
        elif pend_exit:
            st.caption("Ya tienes una solicitud de salida pendiente.")
        else:
            if st.button("🟨 Solicitar salida", key="portal_req_exit", type="primary"):
                try:
                    _ = create_request(con, plate=plate, request_type="EXIT", ttl_minutes=ttl)
                    con.commit()
                    st.success("Solicitud de salida creada ✅")
                    st.rerun()
                except Exception as e:
                    st.error(str(e))

    st.markdown("---")
    st.caption("Nota: el portero verá tu solicitud en Portería (Ingreso / Control) y deberá aceptarla.")

    con.close()


def _fmt(s: str) -> str:
    ts = pd.to_datetime(s, errors="coerce")
    if pd.isna(ts):
        return str(s)[:16]
    return ts.strftime("%Y-%m-%d %H:%M")

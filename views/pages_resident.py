from __future__ import annotations

import streamlit as st

from db.core import db_connect, now_tz
from services.config import get_config
from services.parking import normalize_plate, plate_is_valid
from services.vehicle_portal import verify_vehicle_account
from services.resident_requests import create_request


def page_resident_portal():
    """Portal para que residentes (por placa) soliciten ingreso/salida (requiere aprobación del portero)."""
    con = db_connect()
    st.subheader("🟦 Portal residentes · Solicitudes")

    enabled = int(get_config(con, "resident_portal_enabled", 0) or 0)
    ttl_min = int(get_config(con, "resident_request_ttl_min", 5) or 5)

    if enabled != 1:
        st.info("El portal de residentes está deshabilitado por administración.")
        con.close()
        return

    st.caption(
        "Aquí puedes solicitar **ingreso** o **salida**. La solicitud dura pocos minutos y el portero debe **aprobarla**."
    )

    # sesión simple
    if "RES_plate" not in st.session_state:
        st.session_state["RES_plate"] = ""
    if "RES_authed" not in st.session_state:
        st.session_state["RES_authed"] = False

    c1, c2 = st.columns([2.2, 1.0])
    with c1:
        plate_in = st.text_input("Placa", value=st.session_state.get("RES_plate", ""), key="RES_plate_input")
    with c2:
        if st.session_state.get("RES_authed"):
            if st.button("Cerrar sesión", key="RES_logout"):
                st.session_state["RES_authed"] = False
                st.session_state["RES_plate"] = ""
                st.rerun()

    if not st.session_state.get("RES_authed"):
        pwd = st.text_input("Contraseña", type="password", key="RES_pwd")
        if st.button("Ingresar", key="RES_login", type="primary"):
            plate = normalize_plate(plate_in)
            st.session_state["RES_plate"] = plate
            if not plate_is_valid(plate):
                st.error("Placa inválida")
            elif not verify_vehicle_account(con, plate, pwd):
                st.error("Credenciales inválidas o cuenta inactiva")
            else:
                st.session_state["RES_authed"] = True
                st.success("Listo ✅")
                st.rerun()
        con.close()
        return

    # Autenticado
    plate = normalize_plate(st.session_state.get("RES_plate", ""))
    if not plate_is_valid(plate):
        st.error("Placa inválida")
        st.session_state["RES_authed"] = False
        con.close()
        return

    # estado dentro/fuera
    tk = con.execute(
        """
        SELECT id, entry_time, ticket_type
        FROM tickets
        WHERE plate=? AND exit_time IS NULL
        ORDER BY entry_time DESC
        LIMIT 1
        """,
        (plate,),
    ).fetchone()

    if tk:
        st.success(f"Estado: **DENTRO** desde {str(tk['entry_time'])[:16]}")
    else:
        st.info("Estado: **FUERA** (sin ticket activo)")

    st.markdown("---")

    cA, cB = st.columns(2)
    with cA:
        st.markdown("### 🚪 Solicitar ingreso")
        st.caption(f"Vigencia: {ttl_min} minuto(s)")
        disabled_in = bool(tk)
        if st.button("Enviar solicitud de ingreso", key="RES_req_in", disabled=disabled_in):
            ok, msg = create_request(con, plate, "IN", ttl_min=ttl_min)
            if ok:
                con.commit()
                st.success(msg)
            else:
                st.warning(msg)

    with cB:
        st.markdown("### 🧾 Solicitar salida")
        st.caption(f"Vigencia: {ttl_min} minuto(s)")
        disabled_out = not bool(tk)
        if st.button("Enviar solicitud de salida", key="RES_req_out", disabled=disabled_out):
            ok, msg = create_request(con, plate, "OUT", ttl_min=ttl_min)
            if ok:
                con.commit()
                st.success(msg)
            else:
                st.warning(msg)

    st.markdown("---")
    st.caption("Si el portero no aprueba a tiempo, la solicitud vence automáticamente.")

    con.close()

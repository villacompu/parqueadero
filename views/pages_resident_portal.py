from __future__ import annotations

from datetime import timedelta

import streamlit as st

from db.core import db_connect, now_tz
from services.config import get_config
from services.parking import normalize_plate
from services.resident_portal import (
    verify_vehicle_login,
    create_request,
    get_active_request_for_plate,
    expire_pending_requests,
)


def _time_left(expires_at: str) -> str:
    try:
        exp = now_tz().__class__.fromisoformat(expires_at)  # type: ignore
    except Exception:
        try:
            from datetime import datetime

            exp = datetime.fromisoformat(expires_at)
        except Exception:
            return "-"

    delta = exp - now_tz()
    secs = int(delta.total_seconds())
    if secs <= 0:
        return "0s"
    m, s = divmod(secs, 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}h {m:02d}m"
    return f"{m}m {s:02d}s"


def page_resident_portal():
    """Portal para residentes (login por placa + contraseña)."""
    con = db_connect()

    enabled = bool(get_config(con, "resident_portal_enabled", False))
    allow_entry = bool(get_config(con, "resident_portal_allow_entry", True))
    allow_exit = bool(get_config(con, "resident_portal_allow_exit", True))
    window_min = int(get_config(con, "resident_portal_window_minutes", 5))

    st.subheader("🏠 Portal residentes")
    st.caption("Solicita ingreso o salida. El portero siempre debe aprobar.")

    if not enabled:
        st.info("El Portal de residentes está deshabilitado por Administración.")
        con.close()
        return

    expire_pending_requests(con)
    con.commit()

    # --- sesión portal ---
    if "portal_plate" not in st.session_state:
        st.session_state["portal_plate"] = ""
    if "portal_ok" not in st.session_state:
        st.session_state["portal_ok"] = False

    if not st.session_state.get("portal_ok"):
        with st.form("portal_login"):
            plate_in = st.text_input("Placa", value=st.session_state.get("portal_plate", ""), placeholder="ABC123")
            pwd = st.text_input("Contraseña", type="password")
            ok = st.form_submit_button("Ingresar")

        if ok:
            plate = normalize_plate(plate_in)
            st.session_state["portal_plate"] = plate
            if not plate:
                st.error("Placa inválida")
            elif not pwd:
                st.error("Contraseña obligatoria")
            else:
                if verify_vehicle_login(con, plate, pwd):
                    st.session_state["portal_ok"] = True
                    st.success("Acceso concedido ✅")
                    st.rerun()
                else:
                    st.error("Placa o contraseña incorrectas, o acceso inactivo.")

        st.caption("Si no tienes contraseña, solicita a Administración que habilite el acceso del vehículo.")
        con.close()
        return

    # logged
    plate = st.session_state.get("portal_plate", "")
    st.markdown(f"**Vehículo:** `{plate}`")

    # validar que sea un vehículo residente
    rv = con.execute(
        """
        SELECT rv.plate, rv.vehicle_type, rv.is_active, t.tower_num, a.apt_number
        FROM resident_vehicles rv
        JOIN apartments a ON a.id=rv.apt_id
        JOIN towers t ON t.id=a.tower_id
        WHERE rv.plate=?
        """,
        (plate,),
    ).fetchone()

    if not rv or int(rv["is_active"]) != 1:
        st.error("Este vehículo no está registrado como residente o está inactivo. Contacta a Administración.")
        if st.button("Cerrar sesión portal"):
            st.session_state["portal_ok"] = False
            st.rerun()
        con.close()
        return

    apt_key = f"T{int(rv['tower_num'])}-{rv['apt_number']}"
    st.caption(f"Apartamento: **{apt_key}** · Tipo: **{rv['vehicle_type']}**")

    # estado dentro/fuera
    tk = con.execute(
        "SELECT id, entry_time FROM tickets WHERE plate=? AND exit_time IS NULL ORDER BY entry_time DESC LIMIT 1",
        (plate,),
    ).fetchone()
    inside = bool(tk)

    colA, colB = st.columns(2)

    # ENTRY
    with colA:
        st.markdown("### 🚪 Solicitar ingreso")
        if not allow_entry:
            st.info("Función deshabilitada por Administración.")
        elif inside:
            st.warning("El vehículo aparece como **dentro**. Para salir, usa la solicitud de salida.")
        else:
            ex = get_active_request_for_plate(con, plate, "ENTRY")
            if ex:
                st.success(f"Ya tienes una solicitud *pendiente*. Vence en **{_time_left(ex['expires_at'])}**")
                st.caption(f"Solicitada: {str(ex['requested_at'])[:16]}")
            else:
                if st.button(f"Solicitar ingreso (vigencia {window_min} min)"):
                    rid = create_request(con, plate, "ENTRY", window_min)
                    con.commit()
                    st.success(f"Solicitud enviada ✅ (#{rid}). El portero debe aprobarla.")
                    st.rerun()

    # EXIT
    with colB:
        st.markdown("### 🏁 Solicitar salida")
        if not allow_exit:
            st.info("Función deshabilitada por Administración.")
        elif not inside:
            st.warning("El vehículo aparece como **fuera**. Para entrar, usa la solicitud de ingreso.")
        else:
            ex = get_active_request_for_plate(con, plate, "EXIT")
            if ex:
                st.success(f"Ya tienes una solicitud *pendiente*. Vence en **{_time_left(ex['expires_at'])}**")
                st.caption(f"Solicitada: {str(ex['requested_at'])[:16]}")
            else:
                if st.button(f"Solicitar salida (vigencia {window_min} min)"):
                    rid = create_request(con, plate, "EXIT", window_min)
                    con.commit()
                    st.success(f"Solicitud enviada ✅ (#{rid}). El portero debe aprobarla.")
                    st.rerun()

    st.markdown("---")
    if st.button("Cerrar sesión portal"):
        st.session_state["portal_ok"] = False
        st.rerun()

    con.close()

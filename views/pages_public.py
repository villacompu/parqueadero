from __future__ import annotations

import streamlit as st

from db.core import db_connect, now_tz
from services.parking import zone_used
from views.components import card_metric


def page_public():
    con = db_connect()

    st.markdown("## 🚦 Cupos disponibles ahora")
    st.caption(f"Actualizado: {now_tz().strftime('%H:%M')}")

    zones = con.execute(
        "SELECT id, vehicle_type, capacity FROM zones WHERE is_active=1 AND is_public=1"
    ).fetchall()

    if not zones:
        st.info("Aún no hay zonas públicas configuradas.")
        con.close()
        return

    total_car_cap = total_car_used = 0
    total_moto_cap = total_moto_used = 0

    for z in zones:
        used = zone_used(con, int(z["id"]))
        cap = int(z["capacity"])
        if z["vehicle_type"] == "CAR":
            total_car_cap += cap
            total_car_used += used
        else:
            total_moto_cap += cap
            total_moto_used += used

    car_av = max(total_car_cap - total_car_used, 0)
    moto_av = max(total_moto_cap - total_moto_used, 0)

    st.markdown('<div class="ppn-row">', unsafe_allow_html=True)
    card_metric("🚗 Carros", str(car_av), f"Ocupados: {total_car_used}")
    card_metric("🏍️ Motos", str(moto_av), f"Ocupados: {total_moto_used}")
    st.markdown("</div>", unsafe_allow_html=True)

    st.caption("Revisa antes de entrar a la unidad.")
    con.close()

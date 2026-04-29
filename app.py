from __future__ import annotations

import streamlit as st

from auth.auth_service import current_user, login, logout
from auth import auth_service
from db.core import init_db, db_connect
from services.audit import count_open_incidents
from views.components import inject_css

from views.pages_public import page_public
from views.pages_gate import page_gate_in, page_gate_control, page_end_day
from views.pages_auditor import page_sanctions, page_incidents, page_guard_audit
from views.pages_admin import admin_structure, admin_users, admin_config


# -----------------------------
# Nav helper (✅ evita modificar nav_key después del radio)
# -----------------------------
def request_nav(target_key: str) -> None:
    st.session_state["_nav_request"] = target_key


def apply_pending_nav(valid_keys: list[str]) -> None:
    """
    Aplica la navegación solicitada ANTES de instanciar el widget del menú.
    """
    req = st.session_state.pop("_nav_request", None)
    if req and req in valid_keys:
        st.session_state["nav_key"] = req


# -----------------------------
# Top bar
# -----------------------------
def top_bar() -> None:
    u = current_user()

    # Más ancho para el título, para evitar que “salte” a varias líneas
    c1, c2 = st.columns([7, 1.5])
    with c1:
        if u:
            st.info(f"Sesión: **{u.full_name}** · Rol: **{u.role}**")
        else:
            st.markdown("### 🚗 Parking PH · Primitiva Parque Natural")
            st.caption("Control de accesos · incidencias · sanciones")

    with c2:
        if u and st.button("Cerrar sesión", key="logout_btn", use_container_width=True):
            logout()
            request_nav("PUBLIC")
            st.rerun()

    st.markdown("---")




# -----------------------------
# Login page (más “bonita”)
# -----------------------------
def page_login() -> None:
    st.markdown("## 🔐 Iniciar sesión")
    st.caption("Ingresa con tu usuario y contraseña asignados por administración.")

    left, mid, right = st.columns([1.3, 1.6, 1.3])
    with mid:
        with st.container(border=True):
            with st.form("login", clear_on_submit=False):
                user = st.text_input("Usuario", key="login_user")
                pwd = st.text_input("Contraseña", type="password", key="login_pwd")
                ok = st.form_submit_button("Ingresar", use_container_width=True)

            if ok:
                u = login(user.strip(), pwd)
                if not u:
                    st.error("Usuario o contraseña incorrectos, o usuario inactivo.")
                    return

                st.session_state["user"] = u

                from services.audit import audit
                audit("LOGIN", u.id, {"username": u.username, "role": u.role})

                # ✅ Solo solicitamos navegación (no tocamos nav_key directo)
                if u.role in ("PORTERO", "ADMIN"):
                    request_nav("GIN")
                elif u.role == "AUDITOR":
                    request_nav("SAN")
                else:
                    request_nav("PUBLIC")

                st.success("Sesión iniciada ✅")
                st.rerun()

        ### st.caption("Usuarios por defecto: admin/admin123 · portero1/portero123 · auditor1/auditor123")


# -----------------------------
# Navigation items
# -----------------------------
def build_nav():
    u = current_user()
    items = [("Dashboard público", "PUBLIC")]

    if not u:
        items.append(("Iniciar sesión", "LOGIN"))
        return items

    if u.role in ("PORTERO", "ADMIN"):
        items += [
            ("Portería · Ingreso", "GIN"),
            ("Portería · Control (Dentro + Salida)", "CTRL"),
            ("Portería · Cierre del día", "END"),
        ]

    if u.role in ("AUDITOR", "ADMIN"):
        con = db_connect()
        n_open = count_open_incidents(con)
        con.close()

        label_inc = f"Auditor · Incidencias ({n_open})" if n_open > 0 else "Auditor · Incidencias"
        items += [
            ("Auditor · Sanciones", "SAN"),
            (label_inc, "INC"),
            ("Auditor · Seguimiento", "GAU"),
        ]

    if u.role == "ADMIN":
        items += [
            ("Admin · Estructura", "ASTR"),
            ("Admin · Usuarios", "AUS"),
            ("Admin · Config", "ACFG"),
        ]

    return items


# -----------------------------
# Router
# -----------------------------
def route(key: str) -> None:
    try:
        if key == "PUBLIC":
            page_public()
        elif key == "LOGIN":
            page_login()
        elif key == "GIN":
            page_gate_in()
        elif key == "CTRL":
            page_gate_control()
        elif key == "END":
            page_end_day()
        elif key == "SAN":
            page_sanctions()
        elif key == "INC":
            page_incidents()
        elif key == "GAU":
            page_guard_audit()
        elif key == "ASTR":
            admin_structure()
        elif key == "AUS":
            admin_users()
        elif key == "ACFG":
            admin_config()
        else:
            st.error("Ruta no encontrada.")
    except Exception as e:
        st.error("Ocurrió un error en la pantalla. Abajo está el detalle para corregirlo.")
        st.exception(e)


def main() -> None:
    try:
        st.set_page_config(
            page_title="Parking PH - Primitiva",
            layout="wide",
            initial_sidebar_state="collapsed",
        )
    except Exception:
        pass

    # Init DB
    init_db(auth_service.make_password)

    inject_css()
    top_bar()

    nav = build_nav()
    keys = [x[1] for x in nav]
    label_by_key = {k: l for (l, k) in nav}

    # Default nav
    if "nav_key" not in st.session_state:
        st.session_state["nav_key"] = "PUBLIC"

    # Si lo guardado ya no existe (cambió rol/menú), corregimos
    if st.session_state["nav_key"] not in keys:
        st.session_state["nav_key"] = keys[0]

    # Aplicar navegación solicitada (ANTES del radio)
    apply_pending_nav(keys)

    with st.sidebar:
        st.header("📌 Menú")
        selected = st.radio(
            "Ir a:",
            keys,
            format_func=lambda k: label_by_key.get(k, k),
            key="nav_key",
        )
        st.markdown("---")
        st.caption("Residentes pueden mirar cupos en **Dashboard público** sin iniciar sesión.")

    route(selected)


if __name__ == "__main__":
    main()
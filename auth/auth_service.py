from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from typing import Optional, Tuple

import streamlit as st

from db.core import db_connect, db_query
from services.audit import audit


def _pbkdf2(password: str, salt_hex: str) -> str:
    salt = bytes.fromhex(salt_hex)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 120_000)
    return dk.hex()


def make_password(password: str) -> Tuple[str, str]:
    salt_hex = secrets.token_hex(16)
    return _pbkdf2(password, salt_hex), salt_hex


def verify_password(password: str, password_hash: str, salt_hex: str) -> bool:
    return secrets.compare_digest(_pbkdf2(password, salt_hex), password_hash)


@dataclass
class SessionUser:
    id: int
    username: str
    full_name: str
    role: str


def login(username: str, password: str) -> Optional[SessionUser]:
    con = db_connect()
    rows = db_query(con, "SELECT * FROM users WHERE username=? AND is_active=1", (username,))
    con.close()
    if not rows:
        return None
    r = rows[0]
    if verify_password(password, r["password_hash"], r["salt_hex"]):
        return SessionUser(r["id"], r["username"], r["full_name"] or r["username"], r["role"])
    return None


def current_user() -> Optional[SessionUser]:
    return st.session_state.get("user")


def require_role(*roles: str):
    u = current_user()
    if not u or u.role not in roles:
        st.error("No tienes permisos para ver esta sección.")
        st.stop()


def logout():
    u = current_user()
    if u:
        audit("LOGOUT", u.id, {})
    st.session_state.pop("user", None)

from __future__ import annotations

import sqlite3

from auth.auth_service import make_password, verify_password
from services.parking import normalize_plate, plate_is_valid


def upsert_vehicle_account(con: sqlite3.Connection, plate_in: str, password: str, is_active: int = 1) -> None:
    """Crea o actualiza una cuenta de portal por placa."""
    plate = normalize_plate(plate_in)
    if not plate_is_valid(plate):
        raise ValueError("Placa inválida")
    if not password:
        raise ValueError("Contraseña obligatoria")

    ph, salt = make_password(password)
    now = con.execute("SELECT datetime('now') as ts").fetchone()[0]

    try:
        con.execute(
            """
            INSERT INTO vehicle_accounts(plate,password_hash,salt_hex,is_active,created_at,updated_at)
            VALUES (?,?,?,?,?,?)
            """,
            (plate, ph, salt, int(is_active), now, now),
        )
    except sqlite3.IntegrityError:
        con.execute(
            """
            UPDATE vehicle_accounts
            SET password_hash=?, salt_hex=?, is_active=?, updated_at=?
            WHERE plate=?
            """,
            (ph, salt, int(is_active), now, plate),
        )


def set_vehicle_account_active(con: sqlite3.Connection, plate_in: str, is_active: int) -> None:
    plate = normalize_plate(plate_in)
    if not plate_is_valid(plate):
        raise ValueError("Placa inválida")
    now = con.execute("SELECT datetime('now') as ts").fetchone()[0]
    con.execute(
        "UPDATE vehicle_accounts SET is_active=?, updated_at=? WHERE plate=?",
        (int(is_active), now, plate),
    )


def verify_vehicle_account(con: sqlite3.Connection, plate_in: str, password: str) -> bool:
    plate = normalize_plate(plate_in)
    if not plate_is_valid(plate):
        return False
    row = con.execute(
        "SELECT plate,password_hash,salt_hex,is_active FROM vehicle_accounts WHERE plate=?",
        (plate,),
    ).fetchone()
    if not row:
        return False
    if int(row[3]) != 1:
        return False
    return bool(verify_password(password or "", row[1], row[2]))

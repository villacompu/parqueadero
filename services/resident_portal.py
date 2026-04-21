from __future__ import annotations

from datetime import datetime, timedelta
import re

from auth.auth_service import make_password, verify_password
from db.core import now_tz
from services.parking import normalize_plate


def _now_iso() -> str:
    return now_tz().isoformat()


def ensure_portal_tables(con) -> None:
    """Defensivo: por si se llama antes de init_db (no debería), crea tablas mínimas."""
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS resident_vehicle_accounts(
            plate TEXT PRIMARY KEY,
            password_hash TEXT NOT NULL,
            salt_hex TEXT NOT NULL,
            is_active INTEGER DEFAULT 1,
            created_at TEXT,
            updated_at TEXT
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS resident_portal_requests(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plate TEXT NOT NULL,
            request_type TEXT NOT NULL,
            requested_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            status TEXT NOT NULL,
            approved_by INTEGER,
            approved_at TEXT,
            ticket_id INTEGER,
            notes TEXT
        )
        """
    )


# -----------------------------
# Cuentas por vehículo
# -----------------------------

def upsert_vehicle_account(con, plate: str, password: str | None, is_active: int = 1) -> None:
    """Crea/actualiza cuenta del portal para una placa. Si password es None/"", solo actualiza is_active."""
    ensure_portal_tables(con)

    p = normalize_plate(plate)
    if not p:
        raise ValueError("Placa inválida")

    now = _now_iso()

    existing = con.execute(
        "SELECT plate, password_hash, salt_hex FROM resident_vehicle_accounts WHERE plate=?",
        (p,),
    ).fetchone()

    if password and str(password).strip():
        ph, salt = make_password(str(password).strip())
        if existing:
            con.execute(
                """
                UPDATE resident_vehicle_accounts
                SET password_hash=?, salt_hex=?, is_active=?, updated_at=?
                WHERE plate=?
                """,
                (ph, salt, int(is_active), now, p),
            )
        else:
            con.execute(
                """
                INSERT INTO resident_vehicle_accounts(plate,password_hash,salt_hex,is_active,created_at,updated_at)
                VALUES (?,?,?,?,?,?)
                """,
                (p, ph, salt, int(is_active), now, now),
            )
    else:
        # solo toggle
        if existing:
            con.execute(
                "UPDATE resident_vehicle_accounts SET is_active=?, updated_at=? WHERE plate=?",
                (int(is_active), now, p),
            )
        else:
            raise ValueError("Primero define una contraseña para crear el acceso del vehículo")


def get_vehicle_account(con, plate: str) -> dict | None:
    ensure_portal_tables(con)
    p = normalize_plate(plate)
    row = con.execute(
        "SELECT plate,is_active,created_at,updated_at FROM resident_vehicle_accounts WHERE plate=?",
        (p,),
    ).fetchone()
    return dict(row) if row else None


def verify_vehicle_login(con, plate: str, password: str) -> bool:
    ensure_portal_tables(con)
    p = normalize_plate(plate)
    row = con.execute(
        "SELECT password_hash,salt_hex,is_active FROM resident_vehicle_accounts WHERE plate=?",
        (p,),
    ).fetchone()
    if not row:
        return False
    if int(row["is_active"]) != 1:
        return False
    return bool(verify_password(password or "", row["password_hash"], row["salt_hex"]))


# -----------------------------
# Solicitudes
# -----------------------------

def expire_pending_requests(con) -> int:
    ensure_portal_tables(con)
    now = _now_iso()
    cur = con.execute(
        """
        UPDATE resident_portal_requests
        SET status='EXPIRED'
        WHERE status='PENDING' AND expires_at < ?
        """,
        (now,),
    )
    return int(cur.rowcount or 0)


def get_pending_requests(con, request_type: str, limit: int = 50) -> list[dict]:
    ensure_portal_tables(con)
    expire_pending_requests(con)
    now = _now_iso()
    rows = con.execute(
        """
        SELECT *
        FROM resident_portal_requests
        WHERE status='PENDING' AND request_type=? AND expires_at >= ?
        ORDER BY requested_at DESC
        LIMIT ?
        """,
        (request_type, now, int(limit)),
    ).fetchall()
    return [dict(r) for r in rows]


def get_active_request_for_plate(con, plate: str, request_type: str) -> dict | None:
    ensure_portal_tables(con)
    expire_pending_requests(con)
    p = normalize_plate(plate)
    now = _now_iso()
    row = con.execute(
        """
        SELECT *
        FROM resident_portal_requests
        WHERE plate=? AND request_type=? AND status='PENDING' AND expires_at >= ?
        ORDER BY requested_at DESC
        LIMIT 1
        """,
        (p, request_type, now),
    ).fetchone()
    return dict(row) if row else None


def create_request(con, plate: str, request_type: str, window_minutes: int) -> int:
    ensure_portal_tables(con)
    expire_pending_requests(con)

    p = normalize_plate(plate)
    if request_type not in ("ENTRY", "EXIT"):
        raise ValueError("request_type inválido")

    existing = get_active_request_for_plate(con, p, request_type)
    if existing:
        return int(existing["id"])

    now_dt = now_tz()
    exp = now_dt + timedelta(minutes=int(max(window_minutes, 1)))

    cur = con.execute(
        """
        INSERT INTO resident_portal_requests(plate,request_type,requested_at,expires_at,status)
        VALUES (?,?,?,?, 'PENDING')
        """,
        (p, request_type, now_dt.isoformat(), exp.isoformat()),
    )
    return int(cur.lastrowid)


def approve_request(con, request_id: int, approved_by: int, ticket_id: int | None = None, notes: str | None = None) -> None:
    ensure_portal_tables(con)
    con.execute(
        """
        UPDATE resident_portal_requests
        SET status='APPROVED', approved_by=?, approved_at=?, ticket_id=?, notes=?
        WHERE id=?
        """,
        (int(approved_by), _now_iso(), int(ticket_id) if ticket_id is not None else None, notes, int(request_id)),
    )


def reject_request(con, request_id: int, approved_by: int, notes: str | None = None) -> None:
    ensure_portal_tables(con)
    con.execute(
        """
        UPDATE resident_portal_requests
        SET status='REJECTED', approved_by=?, approved_at=?, notes=?
        WHERE id=?
        """,
        (int(approved_by), _now_iso(), notes, int(request_id)),
    )

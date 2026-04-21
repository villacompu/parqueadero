from __future__ import annotations

import sqlite3
from datetime import timedelta

import pandas as pd

from db.core import now_tz
from services.parking import normalize_plate, plate_is_valid


def _expire_old(con: sqlite3.Connection) -> None:
    """Marca como EXPIRED las solicitudes vencidas."""
    try:
        con.execute(
            """
            UPDATE resident_requests
            SET status='EXPIRED'
            WHERE status='PENDING' AND expires_at < ?
            """,
            (now_tz().isoformat(),),
        )
    except Exception:
        pass


def create_request(con: sqlite3.Connection, plate_in: str, request_type: str, ttl_min: int = 5) -> tuple[bool, str]:
    """Crea una solicitud PENDING. Devuelve (ok, mensaje)."""
    plate = normalize_plate(plate_in)
    if not plate_is_valid(plate):
        return False, "Placa inválida"

    request_type = (request_type or "").upper().strip()
    if request_type not in ("IN", "OUT"):
        return False, "Tipo de solicitud inválido"

    ttl_min = max(int(ttl_min or 5), 1)

    _expire_old(con)

    # solo permitir si la placa está registrada como residente activa
    rv = con.execute(
        "SELECT plate FROM resident_vehicles WHERE plate=? AND is_active=1",
        (plate,),
    ).fetchone()
    if not rv:
        return False, "Placa no registrada como vehículo residente"

    # si ya hay una pendiente, no duplicar
    existing = con.execute(
        """
        SELECT id, expires_at
        FROM resident_requests
        WHERE plate=? AND request_type=? AND status='PENDING'
        ORDER BY id DESC
        LIMIT 1
        """,
        (plate, request_type),
    ).fetchone()
    if existing:
        return False, f"Ya existe una solicitud pendiente (vence {str(existing['expires_at'])[:16] if hasattr(existing,'keys') else str(existing[1])[:16]})."

    created = now_tz()
    expires = created + timedelta(minutes=ttl_min)

    con.execute(
        """
        INSERT INTO resident_requests(plate,request_type,status,created_at,expires_at)
        VALUES (?,?,?,?,?)
        """,
        (plate, request_type, "PENDING", created.isoformat(), expires.isoformat()),
    )
    return True, "Solicitud creada ✅"


def list_pending(con: sqlite3.Connection, request_type: str | None = None, limit: int = 50):
    """Lista solicitudes PENDING no vencidas (join con apto para UI portería)."""
    _expire_old(con)

    request_type = (request_type or "").upper().strip()
    where = "WHERE rr.status='PENDING' AND rr.expires_at >= ?"
    params = [now_tz().isoformat()]
    if request_type in ("IN", "OUT"):
        where += " AND rr.request_type=?"
        params.append(request_type)

    q = f"""
    SELECT rr.*, rv.vehicle_type,
           t.tower_num, a.apt_number, a.resident_name, a.whatsapp
    FROM resident_requests rr
    LEFT JOIN resident_vehicles rv ON rv.plate=rr.plate
    LEFT JOIN apartments a ON a.id=rv.apt_id
    LEFT JOIN towers t ON t.id=a.tower_id
    {where}
    ORDER BY rr.created_at DESC
    LIMIT ?
    """
    params.append(int(limit))
    return con.execute(q, tuple(params)).fetchall()


def approve(con: sqlite3.Connection, request_id: int, user_id: int | None = None, notes: str | None = None) -> None:
    _expire_old(con)
    con.execute(
        """
        UPDATE resident_requests
        SET status='APPROVED', approved_at=?, approved_by=?, notes=?
        WHERE id=? AND status='PENDING'
        """,
        (now_tz().isoformat(), int(user_id) if user_id is not None else None, notes, int(request_id)),
    )


def reject(con: sqlite3.Connection, request_id: int, user_id: int | None = None, notes: str | None = None) -> None:
    _expire_old(con)
    con.execute(
        """
        UPDATE resident_requests
        SET status='REJECTED', approved_at=?, approved_by=?, notes=?
        WHERE id=? AND status='PENDING'
        """,
        (now_tz().isoformat(), int(user_id) if user_id is not None else None, notes, int(request_id)),
    )

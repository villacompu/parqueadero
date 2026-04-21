from __future__ import annotations

import re
from typing import Dict, List, Tuple, Optional

from db.core import db_query
from services.config import get_config


POLICY_PRESETS = ["CAR_ONLY", "MOTO_ONLY_2", "MOTO_ONLY_3", "MIXED_1C1M"]
VEHICLE_TYPES = ["CAR", "MOTO"]


def _row_get(row, key: str, default=None):
    """Compat: sqlite3.Row no tiene .get()."""
    try:
        if row is None:
            return default
        if hasattr(row, "get"):
            return row.get(key, default)
        if hasattr(row, "keys") and key in row.keys():
            return row[key]
    except Exception:
        pass
    return default


def apartment_contact(apt, purpose: str = "VISITOR") -> Tuple[str, str]:
    """
    Retorna (nombre, whatsapp) según propósito.

    - VISITOR: contacto de portería (quien vive). Si arrendado → inquilino.
    - SANCTION: contacto para sanción/preaviso. Si arrendado → dueño.

    Nota: hace fallback a resident_name/whatsapp para compatibilidad.
    """
    is_rented = int(_row_get(apt, "is_rented", 0) or 0)

    if purpose.upper() == "SANCTION":
        if is_rented == 1:
            name = (_row_get(apt, "owner_name") or _row_get(apt, "resident_name") or "").strip()
            wa = (_row_get(apt, "owner_whatsapp") or _row_get(apt, "whatsapp") or "").strip()
            return (name, wa)
        name = (_row_get(apt, "resident_name") or "").strip()
        wa = (_row_get(apt, "whatsapp") or "").strip()
        return (name, wa)

    # VISITOR / default
    if is_rented == 1:
        name = (_row_get(apt, "tenant_name") or _row_get(apt, "resident_name") or "").strip()
        wa = (_row_get(apt, "tenant_whatsapp") or _row_get(apt, "whatsapp") or "").strip()
        return (name, wa)

    name = (_row_get(apt, "resident_name") or "").strip()
    wa = (_row_get(apt, "whatsapp") or "").strip()
    return (name, wa)


def normalize_plate(s: str) -> str:
    return (s or "").strip().upper().replace(" ", "").replace("-", "")


def plate_is_valid(s: str) -> bool:
    s = normalize_plate(s)
    return bool(re.fullmatch(r"[A-Z0-9]{5,7}", s))


def policy_capacity(policy: str) -> Tuple[int, int]:
    if policy == "CAR_ONLY":
        return (1, 0)
    if policy == "MOTO_ONLY_2":
        return (0, 2)
    if policy == "MOTO_ONLY_3":
        return (0, 3)
    if policy == "MIXED_1C1M":
        return (1, 1)
    return (1, 0)


def can_fit_private(policy: str, cars_in: int, motos_in: int, incoming: str) -> bool:
    max_c, max_m = policy_capacity(policy)
    if incoming == "CAR":
        return cars_in + 1 <= max_c and motos_in <= max_m
    return cars_in <= max_c and motos_in + 1 <= max_m


def zone_thresholds(z) -> Dict[str, float]:
    default = {"l1": 0.70, "l2": 0.85, "l3": 0.95}
    try:
        t = json_loads(z["thresholds_json"], default) if z else default
    except Exception:
        t = default
    for k in default:
        if k not in t:
            t[k] = default[k]
    return t


def json_loads(s: str, default=None):
    import json
    try:
        return json.loads(s) if s else default
    except Exception:
        return default


def zone_level(pct: float, th: Dict[str, float]) -> int:
    if pct < th["l1"]:
        return 1
    if pct < th["l2"]:
        return 2
    if pct < th["l3"]:
        return 3
    return 4


def level_badge(level: int) -> str:
    return {1: "🟢", 2: "🟡", 3: "🟠", 4: "🔴"}.get(level, "⚪")


def zone_group_from_name(name: str) -> str:
    m = re.search(r"Etapa\s*(\d+)", name or "", re.IGNORECASE)
    if m:
        return f"Etapa {m.group(1)}"
    return "Otras"

def extract_etapa_number(name: str) -> Optional[int]:
    """Extract etapa number from a zone name like 'Etapa 2 - ...' or 'Zona Visitantes Etapa 2 ...'."""
    if not name:
        return None
    m = re.search(r"Etapa\s*(\d+)", name, re.IGNORECASE)
    if not m:
        return None
    try:
        return int(m.group(1))
    except Exception:
        return None


def get_zone_etapa(z) -> Optional[int]:
    """Return etapa for a zone row/dict using explicit column (etapa_zone) or by parsing the name."""
    try:
        v = z["etapa_zone"]
        if v is not None and str(v).strip() != "":
            return int(v)
    except Exception:
        pass
    try:
        return extract_etapa_number(z["name"])
    except Exception:
        return None


# -----------------------------
# DB queries
# -----------------------------
def get_active_ticket_by_plate(con, plate: str):
    r = db_query(con, "SELECT * FROM tickets WHERE plate=? AND exit_time IS NULL", (plate,))
    return r[0] if r else None


def get_apartment(con, tower_num: int, apt_number: str):
    r = db_query(
        con,
        """
        SELECT a.*, t.tower_num, t.etapa_residencial
        FROM apartments a
        JOIN towers t ON t.id=a.tower_id
        WHERE t.tower_num=? AND a.apt_number=?
          AND COALESCE(a.is_active,1)=1 AND COALESCE(t.is_active,1)=1
        """,
        (tower_num, apt_number),
    )
    return r[0] if r else None


def get_apartment_by_id(con, apt_id: int):
    r = db_query(
        con,
        """
        SELECT a.*, t.tower_num, t.etapa_residencial
        FROM apartments a
        JOIN towers t ON t.id=a.tower_id
        WHERE a.id=?
          AND COALESCE(a.is_active,1)=1 AND COALESCE(t.is_active,1)=1
        """,
        (apt_id,),
    )
    return r[0] if r else None


def get_resident_vehicle(con, plate: str):
    plate = normalize_plate(plate)
    r = db_query(
        con,
        """
        SELECT rv.*, a.has_private_parking, a.apt_number, t.tower_num, t.etapa_residencial, a.tower_id
        FROM resident_vehicles rv
        JOIN apartments a ON a.id=rv.apt_id
        JOIN towers t ON t.id=a.tower_id
        WHERE rv.plate=? AND rv.is_active=1
          AND COALESCE(a.is_active,1)=1 AND COALESCE(t.is_active,1)=1
        """,
        (plate,),
    )
    return r[0] if r else None


def private_cells_for_apt(con, apt_id: int):
    return db_query(
        con,
        """
        SELECT pc.*
        FROM private_cells pc
        JOIN apartments a ON a.id=pc.apt_id
        JOIN towers t ON t.id=a.tower_id
        WHERE pc.apt_id=? AND pc.is_active=1
          AND COALESCE(a.is_active,1)=1 AND COALESCE(t.is_active,1)=1
        ORDER BY pc.code
        """,
        (apt_id,),
    )


def private_cell_occupancy(con, cell_id: int) -> Tuple[int, int]:
    rows = db_query(
        con,
        """
        SELECT vehicle_type, COUNT(*) as n
        FROM tickets
        WHERE private_cell_id=? AND exit_time IS NULL
        GROUP BY vehicle_type
        """,
        (cell_id,),
    )
    cars = 0
    motos = 0
    for r in rows:
        if r["vehicle_type"] == "CAR":
            cars = int(r["n"])
        if r["vehicle_type"] == "MOTO":
            motos = int(r["n"])
    return cars, motos


def zone_used(con, zone_id: int) -> int:
    return int(db_query(con, "SELECT COUNT(*) as n FROM tickets WHERE zone_id=? AND exit_time IS NULL", (zone_id,))[0]["n"])


def active_sanctions(con, apt_id: int, plate: str):
    plate = normalize_plate(plate)
    return db_query(
        con,
        """
        SELECT * FROM sanctions
        WHERE status='ACTIVE'
          AND ((scope='APARTMENT' AND apt_id=?)
               OR (scope='PLATE' AND plate=?))
        ORDER BY created_at DESC
        """,
        (apt_id, plate),
    )


def zone_candidates(con, tower_id: int, vehicle_type: str, profile: str):
    allow_clause = "z.allow_visitors=1" if profile == "VISITOR" else "z.allow_residents_without_private=1"
    return db_query(
        con,
        f"""
        SELECT z.*, zt.priority
        FROM zone_tower zt
        JOIN zones z ON z.id=zt.zone_id
        JOIN towers t ON t.id=zt.tower_id
        WHERE zt.tower_id=? AND z.vehicle_type=?
          AND z.is_active=1 AND z.is_public=1
          AND COALESCE(zt.is_active,1)=1 AND COALESCE(t.is_active,1)=1
          AND {allow_clause}
        ORDER BY zt.priority ASC, z.name ASC
        """,
        (tower_id, vehicle_type),
    )


def best_zone(con, tower_id: int, vehicle_type: str, profile: str):
    """Return (best_zone_row, opts_list).

    Ordering:
      - If tower has explicit zone_tower links: priority ASC, then zones with cup, then lower occupancy.
      - If no links: fallback by etapa distance (same etapa first), then zones with cup, then lower occupancy.
    """
    # tower etapa
    tower_row = db_query(con, "SELECT etapa_residencial FROM towers WHERE id=? AND is_active=1", (tower_id,))
    tower_etapa = int(tower_row[0]["etapa_residencial"]) if tower_row else None

    linked = zone_candidates(con, tower_id, vehicle_type, profile)

    def build_opts(rows, linked_mode: bool):
        out = []
        for z in rows:
            used = zone_used(con, z["id"])
            cap = int(z["capacity"])
            avail = max(cap - used, 0)
            pct = (used / cap) if cap > 0 else 1.0
            lvl = zone_level(pct, zone_thresholds(z))
            etapa_z = get_zone_etapa(z)
            dist = abs(int(etapa_z) - int(tower_etapa)) if (etapa_z is not None and tower_etapa is not None) else 999
            pr = int(z["priority"]) if linked_mode and ("priority" in z.keys()) else 999

            out.append(
                {
                    "id": int(z["id"]),
                    "name": z["name"],
                    "used": int(used),
                    "avail": int(avail),
                    "cap": int(cap),
                    "lvl": int(lvl),
                    "priority": int(pr),
                    "stage_dist": int(dist),
                }
            )
        return out

    if linked:
        opts = build_opts(linked, linked_mode=True)
        opts_sorted = sorted(opts, key=lambda o: (o["priority"], 1 if o["avail"] <= 0 else 0, o["used"] / o["cap"] if o["cap"] else 1.0, o["stage_dist"], o["name"]))
    else:
        # Fallback: all public active zones by vehicle type
        allow_clause = "allow_visitors=1" if profile == "VISITOR" else "allow_residents_without_private=1"
        fallback_rows = db_query(
            con,
            f"""SELECT * FROM zones
                 WHERE is_active=1 AND is_public=1 AND vehicle_type=?
                   AND {allow_clause}
                 ORDER BY name""",
            (vehicle_type,),
        )
        opts = build_opts(fallback_rows, linked_mode=False)
        opts_sorted = sorted(opts, key=lambda o: (o["stage_dist"], 1 if o["avail"] <= 0 else 0, o["used"] / o["cap"] if o["cap"] else 1.0, o["name"]))

    best = None
    for o in opts_sorted:
        if o["avail"] > 0:
            # fetch original row for best if possible
            best = db_query(con, "SELECT * FROM zones WHERE id=?", (o["id"],))
            best = best[0] if best else None
            break
    if best is None and opts_sorted:
        best = db_query(con, "SELECT * FROM zones WHERE id=?", (opts_sorted[0]["id"],))
        best = best[0] if best else None

    return best, opts_sorted

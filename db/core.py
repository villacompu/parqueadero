from __future__ import annotations


import json
import os
import sqlite3
from typing import List, Any, Tuple

from zoneinfo import ZoneInfo
from datetime import datetime

TZ = ZoneInfo("America/Bogota")
DB_PATH = os.environ.get("PARKING_DB_PATH", "parking.db")
UPLOAD_DIR = os.environ.get("PARKING_UPLOAD_DIR", "uploads")


def now_tz() -> datetime:
    return datetime.now(TZ)


def ensure_dirs():
    os.makedirs(UPLOAD_DIR, exist_ok=True)


def db_connect():
    # timeout y pragmas para evitar bloqueos y mejorar concurrencia
    con = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON;")
    try:
        con.execute("PRAGMA journal_mode=WAL;")
        con.execute("PRAGMA synchronous=NORMAL;")
    except Exception:
        pass
    con.execute("PRAGMA busy_timeout = 5000;")
    return con
def db_exec(con, sql: str, params: tuple = ()):
    cur = con.cursor()
    cur.execute(sql, params)
    con.commit()
    return cur


def db_query(con, sql: str, params: tuple = ()) -> List[sqlite3.Row]:
    cur = con.cursor()
    cur.execute(sql, params)
    return cur.fetchall()


def _column_exists(con, table: str, column: str) -> bool:
    rows = db_query(con, f"PRAGMA table_info({table})")
    return any(r["name"] == column for r in rows)


def _add_column_if_missing(con, table: str, col_ddl: str):
    col = col_ddl.split()[0]
    try:
        if _column_exists(con, table, col):
            return
        db_exec(con, f"ALTER TABLE {table} ADD COLUMN {col_ddl}")
    except sqlite3.OperationalError as e:
        if "duplicate column name" in str(e).lower():
            return
        raise


def run_migrations(con):
    _add_column_if_missing(con, "towers", "is_active INTEGER NOT NULL DEFAULT 1")
    _add_column_if_missing(con, "apartments", "is_active INTEGER NOT NULL DEFAULT 1")
    _add_column_if_missing(con, "zone_tower", "is_active INTEGER NOT NULL DEFAULT 1")
    _add_column_if_missing(con, "zones", "etapa_zone INTEGER")
    _add_column_if_missing(con, "apartments", "whatsapp TEXT")

    # Arriendo: datos de dueño e inquilino
    _add_column_if_missing(con, "apartments", "is_rented INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(con, "apartments", "owner_name TEXT")
    _add_column_if_missing(con, "apartments", "owner_whatsapp TEXT")
    _add_column_if_missing(con, "apartments", "tenant_name TEXT")
    _add_column_if_missing(con, "apartments", "tenant_whatsapp TEXT")

    # Incidencias: ticket y evidencia
    _add_column_if_missing(con, "incidents", "ticket_id INTEGER")
    _add_column_if_missing(con, "incidents", "evidence_path TEXT")



def init_db(make_password_fn):
    """Crea tablas si no existen. `make_password_fn` viene de auth.auth_service"""
    ensure_dirs()
    con = db_connect()

    db_exec(con, """
    CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        full_name TEXT,
        role TEXT NOT NULL,
        password_hash TEXT NOT NULL,
        salt_hex TEXT NOT NULL,
        is_active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL
    );
    """)

    db_exec(con, """
    CREATE TABLE IF NOT EXISTS config(
        key TEXT PRIMARY KEY,
        value TEXT
    );
    """)

    db_exec(con, """
    CREATE TABLE IF NOT EXISTS towers(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tower_num INTEGER UNIQUE NOT NULL,
        etapa_residencial INTEGER NOT NULL,
        is_active INTEGER NOT NULL DEFAULT 1
    );
    """)

    db_exec(con, """
    CREATE TABLE IF NOT EXISTS apartments(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tower_id INTEGER NOT NULL,
        apt_number TEXT NOT NULL,
        resident_name TEXT,
        whatsapp TEXT,
        is_rented INTEGER NOT NULL DEFAULT 0,
        owner_name TEXT,
        owner_whatsapp TEXT,
        tenant_name TEXT,
        tenant_whatsapp TEXT,
        has_private_parking INTEGER NOT NULL DEFAULT 0,
        notes TEXT,
        is_active INTEGER NOT NULL DEFAULT 1,
        UNIQUE(tower_id, apt_number),
        FOREIGN KEY(tower_id) REFERENCES towers(id)
    );
    """)

    db_exec(con, """
    CREATE TABLE IF NOT EXISTS resident_vehicles(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        plate TEXT UNIQUE NOT NULL,
        vehicle_type TEXT NOT NULL, -- CAR|MOTO
        apt_id INTEGER NOT NULL,
        brand TEXT,
        color TEXT,
        is_pmr_authorized INTEGER NOT NULL DEFAULT 0,
        is_active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        FOREIGN KEY(apt_id) REFERENCES apartments(id)
    );
    """)

    db_exec(con, """
    CREATE TABLE IF NOT EXISTS zones(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        vehicle_type TEXT NOT NULL, -- CAR|MOTO
        capacity INTEGER NOT NULL,
        is_public INTEGER NOT NULL DEFAULT 1,
        allow_visitors INTEGER NOT NULL DEFAULT 1,
        allow_residents_without_private INTEGER NOT NULL DEFAULT 1,
        thresholds_json TEXT,
            etapa_zone INTEGER,
        is_active INTEGER NOT NULL DEFAULT 1
    );
    """)

    db_exec(con, """
    CREATE TABLE IF NOT EXISTS zone_tower(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        zone_id INTEGER NOT NULL,
        tower_id INTEGER NOT NULL,
        priority INTEGER NOT NULL DEFAULT 1,
        is_active INTEGER NOT NULL DEFAULT 1,
        UNIQUE(zone_id, tower_id),
        FOREIGN KEY(zone_id) REFERENCES zones(id),
        FOREIGN KEY(tower_id) REFERENCES towers(id)
    );
    """)

    db_exec(con, """
    CREATE TABLE IF NOT EXISTS private_cells(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT UNIQUE NOT NULL,
        apt_id INTEGER NOT NULL,
        policy TEXT NOT NULL, -- CAR_ONLY|MOTO_ONLY_2|MOTO_ONLY_3|MIXED_1C1M
        is_pmr INTEGER NOT NULL DEFAULT 0,
        is_active INTEGER NOT NULL DEFAULT 1,
        FOREIGN KEY(apt_id) REFERENCES apartments(id)
    );
    """)

    db_exec(con, """
    CREATE TABLE IF NOT EXISTS tickets(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        plate TEXT NOT NULL,
        vehicle_type TEXT NOT NULL, -- CAR|MOTO
        ticket_type TEXT NOT NULL,  -- RESIDENT|VISITOR
        apt_id INTEGER NOT NULL,
        zone_id INTEGER,
        private_cell_id INTEGER,
        entry_time TEXT NOT NULL,
        exit_time TEXT,
        entry_by INTEGER,
        exit_by INTEGER,
        exit_mode TEXT,
        notes TEXT,
        FOREIGN KEY(apt_id) REFERENCES apartments(id),
        FOREIGN KEY(zone_id) REFERENCES zones(id),
        FOREIGN KEY(private_cell_id) REFERENCES private_cells(id),
        FOREIGN KEY(entry_by) REFERENCES users(id),
        FOREIGN KEY(exit_by) REFERENCES users(id)
    );
    """)

    db_exec(con, """
    CREATE TABLE IF NOT EXISTS sanctions(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        scope TEXT NOT NULL, -- APARTMENT|PLATE
        apt_id INTEGER,
        plate TEXT,
        sanction_type TEXT NOT NULL,
        description TEXT,
        evidence_path TEXT,
        amount REAL,
        block_entry INTEGER NOT NULL DEFAULT 1,
        status TEXT NOT NULL DEFAULT 'ACTIVE',
        created_at TEXT NOT NULL,
        created_by INTEGER NOT NULL,
        closed_at TEXT,
        closed_by INTEGER,
        close_reason TEXT,
        FOREIGN KEY(apt_id) REFERENCES apartments(id),
        FOREIGN KEY(created_by) REFERENCES users(id),
        FOREIGN KEY(closed_by) REFERENCES users(id)
    );
    """)

    db_exec(con, """
    CREATE TABLE IF NOT EXISTS audit_log(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_time TEXT NOT NULL,
        user_id INTEGER,
        action TEXT NOT NULL,
        details_json TEXT,
        FOREIGN KEY(user_id) REFERENCES users(id)
    );
    """)

    db_exec(con, """
    CREATE TABLE IF NOT EXISTS incidents(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticket_id INTEGER NOT NULL,
        plate TEXT NOT NULL,
        vehicle_type TEXT NOT NULL,   -- CAR|MOTO
        ticket_type TEXT NOT NULL,    -- RESIDENT|VISITOR
        apt_id INTEGER NOT NULL,
        zone_id INTEGER,
        private_cell_id INTEGER,
        incident_type TEXT NOT NULL DEFAULT 'END_DAY',
        description TEXT,
        evidence_path TEXT,
        status TEXT NOT NULL DEFAULT 'OPEN',  -- OPEN|CLOSED
        created_at TEXT NOT NULL,
        created_by INTEGER NOT NULL,
        closed_at TEXT,
        closed_by INTEGER,
        close_notes TEXT,
        FOREIGN KEY(ticket_id) REFERENCES tickets(id),
        FOREIGN KEY(apt_id) REFERENCES apartments(id),
        FOREIGN KEY(zone_id) REFERENCES zones(id),
        FOREIGN KEY(private_cell_id) REFERENCES private_cells(id),
        FOREIGN KEY(created_by) REFERENCES users(id),
        FOREIGN KEY(closed_by) REFERENCES users(id)
    );
    """)

    db_exec(con, "CREATE INDEX IF NOT EXISTS idx_incidents_status_created ON incidents(status, created_at);")
    db_exec(con, "CREATE INDEX IF NOT EXISTS idx_incidents_ticket ON incidents(ticket_id, status);")

    # defaults
    def set_cfg_if_missing(key: str, val_json: str):
        r = db_query(con, "SELECT key FROM config WHERE key=?", (key,))
        if not r:
            db_exec(con, "INSERT INTO config(key,value) VALUES (?,?)", (key, val_json))

    set_cfg_if_missing("resident_limits", json.dumps({"max_cars": 1, "max_motos": 2}, ensure_ascii=False))
    set_cfg_if_missing("public_dashboard_title", json.dumps("Cupos de parqueadero - Primitiva Parque Natural", ensure_ascii=False))
    set_cfg_if_missing("end_day_notice_hour", json.dumps(21))
    set_cfg_if_missing("end_day_notice_minute", json.dumps(30))

    # Umbrales cierre del día (minutos)
    set_cfg_if_missing("end_day_threshold_warn_min", json.dumps(60))
    set_cfg_if_missing("end_day_threshold_crit_min", json.dumps(180))

    # Plantilla WhatsApp (Auditor -> Incidencias)
    default_auditor_incident_tpl = (
        "Hola {resident_name}, te escribimos desde Administración / Auditoría de Primitiva Parque Natural.\n\n"
        "Se registró una *incidencia* asociada a tu apartamento Torre {tower_num}, Apto {apt_number}:\n"
        "• Placa: {plate}\n"
        "• Vehículo: {vehicle_type_text} ({vehicle_type})\n"
        "• Lugar/Zona: {place}\n"
        "• Fecha del reporte: {created_at}\n"
        "• Detalle: {description}\n\n"
        "Este mensaje es un *preaviso* (llamado de atención). Si este comportamiento se repite, podría convertirse en una *sanción* "
        "según el reglamento de la copropiedad.\n\n"
        "Por favor ayúdanos confirmando si el visitante ya salió y evitando que vuelva a ocurrir.\n"
        "¡Gracias!"
    )
    set_cfg_if_missing("auditor_incident_whatsapp_template", json.dumps(default_auditor_incident_tpl, ensure_ascii=False))

    # default users if empty
    n = int(db_query(con, "SELECT COUNT(*) as n FROM users")[0]["n"])
    if n == 0:
        def create_user(username, full_name, role, password):
            ph, salt = make_password_fn(password)
            db_exec(con,
                """INSERT INTO users(username,full_name,role,password_hash,salt_hex,is_active,created_at)
                VALUES (?,?,?,?,?,?,?)""",
                (username, full_name, role, ph, salt, 1, now_tz().isoformat()),
            )
        create_user("admin", "Administrador", "ADMIN", "admin123")
        create_user("portero1", "Portero 1", "PORTERO", "portero123")
        create_user("auditor1", "Auditor 1", "AUDITOR", "auditor123")

    run_migrations(con)
    con.close()

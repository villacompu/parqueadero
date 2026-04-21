from __future__ import annotations

import json
from typing import Any

from db.core import db_query, db_exec


def get_config(con, key: str, default=None):
    rows = db_query(con, "SELECT value FROM config WHERE key=?", (key,))
    if not rows:
        return default
    try:
        return json.loads(rows[0]["value"])
    except Exception:
        return default


def set_config(con, key: str, value: Any):
    db_exec(con, "INSERT OR REPLACE INTO config(key,value) VALUES (?,?)", (key, json.dumps(value, ensure_ascii=False)))

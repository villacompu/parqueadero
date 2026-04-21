from __future__ import annotations

import json
from typing import Any, Dict, Optional

from db.core import db_connect, db_exec, now_tz, db_query


def audit(action: str, user_id: Optional[int], details: Dict[str, Any] | None = None):
    con = db_connect()
    db_exec(
        con,
        "INSERT INTO audit_log(event_time,user_id,action,details_json) VALUES (?,?,?,?)",
        (now_tz().isoformat(), user_id, action, json.dumps(details or {}, ensure_ascii=False, default=str)),
    )
    con.close()


def count_open_incidents(con) -> int:
    return int(db_query(con, "SELECT COUNT(*) as n FROM incidents WHERE status='OPEN'")[0]["n"])

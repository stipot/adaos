# tests/test_sql_schema.py
from __future__ import annotations
from adaos.adapters.db.sqlite_store import SQLite
from adaos.adapters.db.sqlite_schema import ensure_schema
from adaos.services.agent_context import get_ctx


def test_sqlite_schema_tables_exist():
    sql = get_ctx().sql
    ensure_schema(sql)
    with sql.connect() as con:
        cur = con.execute("SELECT name FROM sqlite_master WHERE type='table'")
        names = {r[0] for r in cur.fetchall()}
    assert {"skills", "skill_versions", "scenarios", "scenario_versions"} <= names

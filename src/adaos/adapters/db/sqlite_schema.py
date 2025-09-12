# src/adaos/adapters/db/sqlite_schema.py
from __future__ import annotations

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS skills (
        id INTEGER PRIMARY KEY,
        name TEXT UNIQUE,
        active_version TEXT,
        repo_url TEXT,
        installed BOOLEAN DEFAULT 1,
        last_updated TIMESTAMP
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS skill_versions (
        id INTEGER PRIMARY KEY,
        skill_name TEXT,
        version TEXT,
        path TEXT,
        status TEXT,
        created_at TIMESTAMP
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS scenarios (
        id INTEGER PRIMARY KEY,
        name TEXT UNIQUE,
        active_version TEXT,
        repo_url TEXT,
        installed BOOLEAN DEFAULT 1,
        last_updated TIMESTAMP
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS scenario_versions (
        id INTEGER PRIMARY KEY,
        scenario_name TEXT,
        version TEXT,
        path TEXT,
        status TEXT,
        created_at TIMESTAMP
    );
    """,
)


def ensure_schema(sql) -> None:
    with sql.connect() as con:
        cur = con.cursor()
        for stmt in _SCHEMA:
            cur.execute(stmt)
        con.commit()

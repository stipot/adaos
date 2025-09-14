# src\adaos\adapters\db\sqlite_skill_registry.py
from __future__ import annotations
import sqlite3
from typing import Iterable, Optional
from adaos.domain import SkillRecord
from adaos.ports import SQL
from adaos.adapters.db.sqlite_schema import ensure_schema


class SqliteSkillRegistry:
    """Адаптер реестра навыков на базе таблиц `skills`/`skill_versions`."""

    def __init__(self, sql: SQL):
        self.sql = sql
        ensure_schema(self.sql)

    def list(self) -> list[SkillRecord]:
        with self.sql.connect() as con:
            cur = con.execute(
                "SELECT name, active_version, repo_url, installed, " "strftime('%s', COALESCE(last_updated, CURRENT_TIMESTAMP)) " "FROM skills WHERE installed = 1 ORDER BY name"
            )
            rows = cur.fetchall()
        out: list[SkillRecord] = []
        for name, active_version, repo_url, installed, last_updated in rows:
            out.append(
                SkillRecord(
                    name=name,
                    installed=bool(installed),
                    active_version=active_version,
                    repo_url=repo_url,
                    last_updated=float(last_updated) if last_updated is not None else None,
                )
            )
        return out

    def get(self, name: str) -> SkillRecord | None:
        with self.sql.connect() as con:
            cur = con.execute(
                "SELECT name, active_version, repo_url, installed, " "strftime('%s', COALESCE(last_updated, CURRENT_TIMESTAMP)) " "FROM skills WHERE name = ?", (name,)
            )
            row = cur.fetchone()
        if not row:
            return None
        name, active_version, repo_url, installed, last_updated = row
        return SkillRecord(
            name=name,
            installed=bool(installed),
            active_version=active_version,
            repo_url=repo_url,
            last_updated=float(last_updated) if last_updated is not None else None,
        )

    def register(self, name: str, *, pin: str | None = None, active_version: str | None = None, repo_url: str | None = None) -> SkillRecord:
        # installed=1, last_updated=CURRENT_TIMESTAMP
        with self.sql.connect() as con:
            con.execute(
                """
                INSERT INTO skills(name, active_version, repo_url, installed, last_updated)
                VALUES (?, ?, ?, 1, CURRENT_TIMESTAMP)
                ON CONFLICT(name)
                DO UPDATE SET
                    active_version = COALESCE(?, skills.active_version),
                    repo_url       = COALESCE(?, skills.repo_url),
                    installed      = 1,
                    last_updated   = CURRENT_TIMESTAMP
                """,
                (name, active_version, repo_url, active_version, repo_url),
            )
            con.commit()
        rec = self.get(name)
        return SkillRecord(
            name=name,
            installed=True,
            active_version=rec.active_version if rec else active_version,
            repo_url=rec.repo_url if rec else repo_url,
            pin=pin,
            last_updated=rec.last_updated if rec else None,
        )

    def unregister(self, name: str) -> None:
        with self.sql.connect() as con:
            con.execute("UPDATE skills SET installed = 0, last_updated = CURRENT_TIMESTAMP WHERE name = ?", (name,))
            con.commit()

    def set_all(self, records: Iterable[SkillRecord]) -> None:
        names = [(r.name,) for r in records]
        with self.sql.connect() as con:
            con.execute("UPDATE skills SET installed = 0, last_updated = CURRENT_TIMESTAMP WHERE installed = 1")
            if names:
                con.executemany(
                    "INSERT INTO skills(name, installed, last_updated) VALUES(?, 1, CURRENT_TIMESTAMP) "
                    "ON CONFLICT(name) DO UPDATE SET installed = 1, last_updated = CURRENT_TIMESTAMP",
                    names,
                )
            con.commit()

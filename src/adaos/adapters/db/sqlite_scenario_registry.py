# src/adaos/adapters/db/sqlite_scenario_registry.py
from __future__ import annotations
from adaos.adapters.db.sqlite_schema import ensure_schema
from adaos.domain import SkillRecord
from adaos.ports import SQL


class SqliteScenarioRegistry:
    """Реестр сценариев на таблицах `scenarios`/`scenario_versions`."""

    def __init__(self, sql: SQL):
        self.sql = sql
        ensure_schema(self.sql)

    def list(self) -> list[SkillRecord]:
        with self.sql.connect() as con:
            cur = con.execute(
                "SELECT name, active_version, repo_url, installed, " "strftime('%s', COALESCE(last_updated, CURRENT_TIMESTAMP)) " "FROM scenarios WHERE installed = 1 ORDER BY name"
            )
            rows = cur.fetchall()
        return [
            SkillRecord(
                name=row[0],
                installed=bool(row[3]),
                active_version=row[1],
                repo_url=row[2],
                last_updated=float(row[4]) if row[4] is not None else None,
            )
            for row in rows
        ]

    def get(self, name: str) -> SkillRecord | None:
        with self.sql.connect() as con:
            cur = con.execute(
                "SELECT name, active_version, repo_url, installed, " "strftime('%s', COALESCE(last_updated, CURRENT_TIMESTAMP)) " "FROM scenarios WHERE name = ?", (name,)
            )
            row = cur.fetchone()
        if not row:
            return None
        return SkillRecord(
            name=row[0],
            installed=bool(row[3]),
            active_version=row[1],
            repo_url=row[2],
            last_updated=float(row[4]) if row[4] is not None else None,
        )

    def register(self, name: str, *, pin: str | None = None, active_version: str | None = None, repo_url: str | None = None) -> SkillRecord:
        with self.sql.connect() as con:
            con.execute(
                """
                INSERT INTO scenarios(name, active_version, repo_url, installed, last_updated)
                VALUES (?, ?, ?, 1, CURRENT_TIMESTAMP)
                ON CONFLICT(name) DO UPDATE SET
                    active_version = COALESCE(?, scenarios.active_version),
                    repo_url       = COALESCE(?, scenarios.repo_url),
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
            con.execute(
                "UPDATE scenarios SET installed = 0, last_updated = CURRENT_TIMESTAMP WHERE name = ?",
                (name,),
            )
            con.commit()

    def set_all(self, records: list[SkillRecord]) -> None:
        names = [(r.name,) for r in records]
        with self.sql.connect() as con:
            con.execute("UPDATE scenarios SET installed = 0, last_updated = CURRENT_TIMESTAMP WHERE installed = 1")
            if names:
                con.executemany(
                    "INSERT INTO scenarios(name, installed, last_updated) VALUES(?, 1, CURRENT_TIMESTAMP) "
                    "ON CONFLICT(name) DO UPDATE SET installed = 1, last_updated = CURRENT_TIMESTAMP",
                    names,
                )
            con.commit()

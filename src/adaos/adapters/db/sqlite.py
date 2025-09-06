# -*- coding: utf-8 -*-
"""
Лёгкий слой совместимости со старым API:
add_or_update_entity, update_skill_version, list_entities, set_installed_flag.

Внутри использует текущее подключение SQLite из bootstrap (ctx.sql)
и ту же схему таблиц (skills/skill_versions, scenarios/scenario_versions).
"""
from __future__ import annotations
from typing import Optional, Iterable, Literal, List, Dict, Any
from adaos.apps.bootstrap import get_ctx

Entity = Literal["skills", "scenarios"]

_SKILL_VERS = "skill_versions"
_SCEN_VERS = "scenario_versions"


def _vers_table(entity: Entity) -> str:
    return _SCEN_VERS if entity == "scenarios" else _SKILL_VERS


def add_or_update_entity(
    entity: Entity,
    name: str,
    active_version: Optional[str] = None,
    repo_url: Optional[str] = None,
    installed: bool = True,
) -> None:
    """
    Upsert записи в таблицы skills/scenarios (совместимо со старым кодом).
    """
    if entity not in ("skills", "scenarios"):
        raise ValueError("entity must be 'skills' or 'scenarios'")
    sql = get_ctx().sql
    with sql.connect() as con:
        con.execute(
            f"""
            INSERT INTO {entity}(name, active_version, repo_url, installed, last_updated)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(name) DO UPDATE SET
                active_version = COALESCE(?, {entity}.active_version),
                repo_url       = COALESCE(?, {entity}.repo_url),
                installed      = ?,
                last_updated   = CURRENT_TIMESTAMP
            """,
            (name, active_version, repo_url, 1 if installed else 0, active_version, repo_url, 1 if installed else 0),
        )
        con.commit()


def set_installed_flag(entity: Entity, name: str, installed: bool) -> None:
    if entity not in ("skills", "scenarios"):
        raise ValueError("entity must be 'skills' or 'scenarios'")
    sql = get_ctx().sql
    with sql.connect() as con:
        con.execute(
            f"UPDATE {entity} SET installed=?, last_updated=CURRENT_TIMESTAMP WHERE name=?",
            (1 if installed else 0, name),
        )
        con.commit()


def list_entities(entity: Entity, installed_only: bool = True) -> List[Dict[str, Any]]:
    """
    Возвращает список словарей (совместимо с прежним стилем).
    Ключи: name, active_version, repo_url, installed, last_updated (unix).
    """
    if entity not in ("skills", "scenarios"):
        raise ValueError("entity must be 'skills' or 'scenarios'")
    sql = get_ctx().sql
    where = "WHERE installed=1" if installed_only else ""
    with sql.connect() as con:
        cur = con.execute(
            f"""
            SELECT name, active_version, repo_url, installed,
                   strftime('%s', COALESCE(last_updated, CURRENT_TIMESTAMP))
            FROM {entity} {where}
            ORDER BY name
            """
        )
        rows = cur.fetchall()
    return [
        {
            "name": r[0],
            "active_version": r[1],
            "repo_url": r[2],
            "installed": bool(r[3]),
            "last_updated": float(r[4]) if r[4] is not None else None,
        }
        for r in rows
    ]


def update_skill_version(
    entity: Entity,
    name: str,
    version: str,
    path: str,
    status: str = "available",  # например: available/active/disabled
) -> None:
    """
    Добавляет запись о версии в skill_versions/scenario_versions.
    """
    if entity not in ("skills", "scenarios"):
        raise ValueError("entity must be 'skills' or 'scenarios'")
    table = _vers_table(entity)
    sql = get_ctx().sql
    with sql.connect() as con:
        con.execute(
            f"""
            INSERT INTO {table}( { 'skill_name' if entity=='skills' else 'scenario_name' }, version, path, status, created_at)
            VALUES( ?, ?, ?, ?, CURRENT_TIMESTAMP )
            """,
            (name, version, path, status),
        )
        con.commit()


def get_skill_versions(name: str) -> List[Dict[str, Any]]:
    """
    Вернуть версии навыка из таблицы skill_versions.
    Формат элементов: {'version': str, 'path': str, 'status': str, 'created_at': str}
    """
    sql = get_ctx().sql
    with sql.connect() as con:
        cur = con.execute(
            "SELECT version, path, status, COALESCE(created_at, CURRENT_TIMESTAMP) " "FROM skill_versions WHERE skill_name=? ORDER BY created_at DESC, version DESC",
            (name,),
        )
        rows = cur.fetchall()
    return [{"version": r[0], "path": r[1], "status": r[2], "created_at": r[3]} for r in rows]


def list_versions(name: str) -> Optional[str]:
    """
    Совместимый помощник: вернуть active_version навыка из skills.
    Старый CLI ожидает одиночное значение.
    """
    sql = get_ctx().sql
    with sql.connect() as con:
        cur = con.execute("SELECT active_version FROM skills WHERE name=?", (name,))
        row = cur.fetchone()
    return row[0] if row and row[0] else None

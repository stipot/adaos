import sqlite3
import os
from pathlib import Path
from datetime import datetime

DB_PATH = os.getenv("BASE_DIR", str(Path.home())) + "/.adaos/skill_db.sqlite"
SKILLS_DIR = "./runtime/skills"


def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        """
    CREATE TABLE IF NOT EXISTS skills (
        id INTEGER PRIMARY KEY,
        name TEXT UNIQUE,
        active_version TEXT,
        repo_url TEXT,
        installed BOOLEAN DEFAULT 1,
        last_updated TIMESTAMP
    )
    """
    )
    cursor.execute(
        """
    CREATE TABLE IF NOT EXISTS skill_versions (
        id INTEGER PRIMARY KEY,
        skill_name TEXT,
        version TEXT,
        path TEXT,
        status TEXT,
        created_at TIMESTAMP
    )
    """
    )
    # Сканируем папку skills
    if os.path.exists(SKILLS_DIR):
        for skill_name in os.listdir(SKILLS_DIR):
            skill_path = os.path.join(SKILLS_DIR, skill_name)
            if os.path.isdir(skill_path):
                # Проверяем, есть ли навык в базе
                cursor.execute("SELECT id FROM skills WHERE name = ?", (skill_name,))
                exists = cursor.fetchone()

                if not exists:
                    print(f"[INFO] Добавляем навык {skill_name}")
                    cursor.execute("INSERT INTO skills (name, active_version) VALUES (?, ?)", (skill_name, "1.0"))  # версию можно определить из метаданных
    conn.commit()
    conn.close()


def set_installed_flag(name: str, installed: int):
    """Устанавливает или сбрасывает флаг installed для навыка"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("UPDATE skills SET installed=? WHERE name=?", (installed, name))
    conn.commit()
    conn.close()


def list_skills():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT name, active_version FROM skills")
    rows = cursor.fetchall()
    conn.close()
    return [{"name": r[0], "active_version": r[1]} for r in rows]


def get_skill_versions(skill_name: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT version, status FROM skill_versions WHERE skill_name = ?", (skill_name,))
    rows = cursor.fetchall()
    conn.close()
    return [{"version": r[0], "status": r[1]} for r in rows]


def list_versions(skill_name: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT active_version FROM skills WHERE name = ?", (skill_name,))
    row = cursor.fetchone()
    conn.close()

    return row[0] if row else None


def add_skill_version(skill_name: str, version: str, path: str, status: str = "active"):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    now = datetime.now().isoformat()

    # Обновляем таблицу skills
    cursor.execute(
        """
    INSERT INTO skills (name, active_version, last_updated)
    VALUES (?, ?, ?)
    ON CONFLICT(name) DO UPDATE SET active_version=excluded.active_version, last_updated=excluded.last_updated
    """,
        (skill_name, version, now),
    )

    # Добавляем версию
    cursor.execute(
        """
    INSERT INTO skill_versions (skill_name, version, path, status, created_at)
    VALUES (?, ?, ?, ?, ?)
    """,
        (skill_name, version, path, status, now),
    )

    conn.commit()
    conn.close()


def add_or_update_skill(name: str, version: str, repo_url: str, installed: int):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO skills (name, active_version, repo_url, installed)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(name)
        DO UPDATE SET active_version = excluded.active_version, repo_url = excluded.repo_url
    """,
        (name, version, repo_url, installed),
    )
    conn.commit()
    conn.close()


def update_skill_version(name: str, version: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("UPDATE skills SET active_version = ? WHERE name = ?", (version, name))
    conn.commit()
    conn.close()


def get_skill(name: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT name, active_version, repo_url FROM skills WHERE name = ?", (name,))
    row = cursor.fetchone()
    conn.close()
    return {"name": row[0], "active_version": row[1], "repo_url": row[2]} if row else None


def list_skills():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT name, active_version, repo_url FROM skills")
    rows = cursor.fetchall()
    conn.close()
    return [{"name": r[0], "active_version": r[1], "repo_url": r[2]} for r in rows]

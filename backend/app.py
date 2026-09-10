"""trt.Schulmanager (LehrerWerk) — FastAPI-Backend mit SQLite-Persistenz und Login.

Speichert den kompletten App-State als JSON-Dokument in SQLite
(Tabelle `state`, einzelne Zeile) und serviert das Frontend statisch.
WAL-Modus für robusten Betrieb, ein Worker pro Container.
"""
import json
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Set

from fastapi import Body, Depends, FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles

DB_PATH = os.environ.get("DB_PATH", "/data/lehrerwerk.db")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "lehrer2026")

app = FastAPI(title="trt.Schulmanager API", version="2.0.0")
TOKENS: Set[str] = set()


@contextmanager
def db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS state (
                id         INTEGER PRIMARY KEY CHECK (id = 1),
                json       TEXT    NOT NULL,
                updated_at TEXT    NOT NULL
            )
            """
        )
        yield conn
        conn.commit()
    finally:
        conn.close()


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def require_auth(request: Request) -> None:
    auth = request.headers.get("authorization", "")
    token = auth[7:] if auth.startswith("Bearer ") else ""
    if not token or token not in TOKENS:
        raise HTTPException(status_code=401, detail="Nicht angemeldet")


@app.get("/api/health")
def health():
    try:
        with db() as conn:
            conn.execute("SELECT 1").fetchone()
        return {"status": "ok"}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/login")
def login(payload: dict = Body(...)):
    if not isinstance(payload, dict) or payload.get("password") != APP_PASSWORD:
        raise HTTPException(status_code=401, detail="Falsches Passwort")
    token = secrets.token_urlsafe(32)
    TOKENS.add(token)
    return {"ok": True, "token": token}


@app.get("/api/state", dependencies=[Depends(require_auth)])
def get_state():
    with db() as conn:
        row = conn.execute("SELECT json, updated_at FROM state WHERE id = 1").fetchone()
    if not row:
        return {"state": None, "updated_at": None}
    return {"state": json.loads(row[0]), "updated_at": row[1]}


@app.put("/api/state", dependencies=[Depends(require_auth)])
def put_state(payload: dict = Body(...)):
    if not isinstance(payload, dict) or payload.get("version") not in (1, 2):
        raise HTTPException(status_code=400, detail="Ungültiges State-Format (version 1 oder 2 erwartet)")
    now = utcnow()
    data = json.dumps(payload, ensure_ascii=False)
    with db() as conn:
        conn.execute(
            """
            INSERT INTO state (id, json, updated_at) VALUES (1, ?, ?)
            ON CONFLICT (id) DO UPDATE SET json = excluded.json, updated_at = excluded.updated_at
            """,
            (data, now),
        )
    return {"ok": True, "updated_at": now}


@app.delete("/api/state", dependencies=[Depends(require_auth)])
def delete_state():
    with db() as conn:
        conn.execute("DELETE FROM state WHERE id = 1")
    return {"ok": True}


# Statisches Frontend — nach den API-Routen mounten
app.mount("/", StaticFiles(directory="static", html=True), name="static")

"""trt.Schulmanager (LehrerWerk) — FastAPI-Backend mit SQLite, Login und Telegram-Integration.

- Kompletter App-State als JSON-Dokument in SQLite (Tabelle `state`, einzelne Zeile)
- Login mit Passwort aus ENV (APP_PASSWORD), Session-Token im RAM
- Optionale Telegram-Integration (reine Standardbibliothek, keine Zusatz-Dependencies):
  * Bot-Commands: /start /help /status /heute /noten <Klasse>
  * Taegliche Klassenbuch-Erinnerung (Mo-Fr) an TELEGRAM_CHAT_ID
- Statisches Frontend aus /static, WAL-Modus, ein Worker pro Container
"""
import json
import os
import secrets
import sqlite3
import threading
import time
import urllib.request
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Set

from fastapi import Body, Depends, FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles

DB_PATH = os.environ.get("DB_PATH", "/data/lehrerwerk.db")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "lehrer2026")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
REMINDER_TIME = os.environ.get("REMINDER_TIME", "").strip()  # z. B. "17:30", leer = aus

DAYS = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag"]

app = FastAPI(title="trt.Schulmanager API", version="2.1.0")
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


def get_state_raw():
    with db() as conn:
        row = conn.execute("SELECT json FROM state WHERE id = 1").fetchone()
    return json.loads(row[0]) if row else None


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


# ============ Notenlogik (Port aus dem Frontend, fuer Telegram-Befehle) ============

BUILTIN_SCALES = {
    "ihk": {"type": "bounds", "bounds": [92, 81, 67, 50, 30]},
    "punkte": {"type": "punkte"},
    "lin50": {"type": "linear", "pass": 50},
    "lin45": {"type": "linear", "pass": 45},
    "lin40": {"type": "linear", "pass": 40},
}


def get_scale(scale_id, custom_scales):
    if scale_id in BUILTIN_SCALES:
        return BUILTIN_SCALES[scale_id]
    for c in custom_scales or []:
        if c.get("id") == scale_id:
            return c
    return BUILTIN_SCALES["ihk"]


def grade_from_pct(pct: float, sc: dict) -> float:
    pct = max(0.0, min(100.0, pct))
    t = sc.get("type")
    if t == "bounds":
        b = sc.get("bounds", [92, 81, 67, 50, 30])
        for i in range(5):
            if pct >= b[i]:
                upper = 100 if i == 0 else b[i - 1]
                return (i + 1) if pct <= b[i] else (i + 1) + (upper - pct) / (upper - b[i])
        return 6.0
    if t == "punkte":
        p = 15 if pct >= 95 else (int((pct - 25) // 5) + 1 if pct >= 25 else 0)
        return 6 - p / 3
    pass_ = sc.get("pass", 45)
    if pct >= pass_:
        return 1 + 3 * (100 - pct) / (100 - pass_)
    return 4 + 2 * (pass_ - pct) / pass_


def fmt_grade(g: float) -> str:
    return f"{g:.1f}".replace(".", ",")


def exam_grade(ex: dict, sid: str, custom_scales):
    r = (ex.get("results") or {}).get(sid)
    if not r:
        return None
    if r.get("note") is not None:
        return float(r["note"])
    pts = r.get("points")
    if pts is None or pts == "":
        return None
    sc = get_scale(ex.get("scale"), custom_scales)
    sockel = sc.get("sockel") or 0
    mx = ex.get("max") or 1
    eff = min(mx, float(pts) + sockel)
    return grade_from_pct(eff / mx * 100, sc)


def week_key_today() -> str:
    iso = date.today().isocalendar()
    return f"{iso[0]}-{iso[1]:02d}"


# ============ Telegram ============

HELP_TEXT = (
    "🤖 <b>trt.Schulmanager-Bot</b>\n\n"
    "/status — Dashboard-Kennzahlen\n"
    "/heute — heutige Klassenbuchstunden\n"
    "/noten <Klasse> — Zeugnisnoten (z. B. /noten 9b)\n"
    "/help — diese Übersicht"
)


def tg_call(method: str, **params):
    if not TELEGRAM_TOKEN:
        return None
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}",
            data=json.dumps(params).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=40) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as exc:
        print("[telegram]", method, "→", exc)
        return None


def tg_send(text: str, chat_id: str = None):
    cid = chat_id or TELEGRAM_CHAT_ID
    if not TELEGRAM_TOKEN or not cid:
        return
    while text:
        tg_call("sendMessage", chat_id=cid, text=text[:4000], parse_mode="HTML")
        text = text[4000:]


def cmd_status() -> str:
    s = get_state_raw()
    if not s:
        return "Noch keine Daten vorhanden."
    wk = week_key_today()
    lessons = (s.get("lessons") or {}).get(wk) or {}
    classes = s.get("classes") or []
    students = sum(len(c.get("students") or []) for c in classes)
    grades = sum(
        1
        for c in classes
        for ex in (c.get("exams") or [])
        for r in (ex.get("results") or {}).values()
        if r and (r.get("points") is not None or r.get("note") is not None)
    )
    units = s.get("units") or []
    active = [u for u in units if u.get("status") != "Abgeschlossen"]
    kw = date.today().isocalendar()[1]
    return (
        "📊 <b>trt.Schulmanager — Status</b>\n\n"
        f"👥 Klassen: {len(classes)} ({students} Schüler)\n"
        f"📖 Einträge diese Woche (KW {kw}): {len(lessons)}\n"
        f"📝 Noten erfasst: {grades}\n"
        f"🗂️ Einheiten: {len(units)} ({len(active)} aktiv)"
    )


def cmd_heute() -> str:
    today = date.today()
    if today.weekday() >= 5:
        return "🌴 Wochenende — kein Unterricht."
    s = get_state_raw()
    wk = week_key_today()
    lessons = ((s or {}).get("lessons") or {}).get(wk) or {}
    d = today.weekday()
    rows = []
    for k, l in lessons.items():
        if k.startswith(f"{d}-"):
            rows.append((int(k.split("-")[1]), l))
    if not rows:
        return f"📖 <b>Klassenbuch für {DAYS[d]} ist noch leer!</b>"
    rows.sort(key=lambda x: x[0])
    lines = [f"📖 <b>Klassenbuch — {DAYS[d]}, {today.strftime('%d.%m.')}</b>", ""]
    for h, l in rows:
        ha = f"\n   📌 {l['homework']}" if l.get("homework") else ""
        lines.append(f"{h}. Std · <b>{l.get('subject', '')}</b> — {l.get('topic', '')}{ha}")
    return "\n".join(lines)


def cmd_noten(name: str) -> str:
    s = get_state_raw()
    if not s:
        return "Noch keine Daten vorhanden."
    custom = s.get("customScales") or []
    classes = s.get("classes") or []
    if not classes:
        return "Noch keine Klassen angelegt."
    if not name:
        names = ", ".join(c.get("name", "?") for c in classes)
        return f"Verwendung: /noten <Klasse>\nVerfügbar: {names}"
    name_l = name.strip().lower()
    c = next((x for x in classes if name_l in (x.get("name") or "").lower()), None)
    if not c:
        return f"Klasse „{name}“ nicht gefunden."
    students = c.get("students") or []
    if not students:
        return f"Klasse {c.get('name')} hat noch keine Schüler."
    lines = [f"🎓 <b>Zeugnisnoten — {c.get('name')}{(' · ' + c.get('subject')) if c.get('subject') else ''}</b>", ""]
    for st in students:
        sw = sg = 0.0
        for ex in c.get("exams") or []:
            g = exam_grade(ex, st.get("id"), custom)
            if g is None:
                continue
            w = ex.get("weight") or 1
            sw += w
            sg += w * g
        if sw:
            avg = sg / sw
            lines.append(f"• {st.get('name', '?')}: <b>{fmt_grade(avg)}</b> (≈ {round((6 - avg) * 3)} P)")
        else:
            lines.append(f"• {st.get('name', '?')}: –")
    return "\n".join(lines)


def handle_update(upd: dict):
    msg = upd.get("message") or {}
    chat_id = str((msg.get("chat") or {}).get("id") or "")
    text = (msg.get("text") or "").strip()
    if not chat_id or not text:
        return
    if TELEGRAM_CHAT_ID and chat_id != TELEGRAM_CHAT_ID:
        return
    cmd = text.split()[0].split("@")[0].lower()
    if cmd == "/start":
        if not TELEGRAM_CHAT_ID:
            tg_send(
                f"👋 Deine Chat-ID: <code>{chat_id}</code>\n\n"
                "Trage sie im Portainer-Stack als Umgebungsvariable TELEGRAM_CHAT_ID ein "
                "und starte den Container neu.",
                chat_id,
            )
        else:
            tg_send("✅ Verbunden.\n\n" + HELP_TEXT, chat_id)
    elif cmd == "/help":
        tg_send(HELP_TEXT, chat_id)
    elif cmd == "/status":
        tg_send(cmd_status(), chat_id)
    elif cmd == "/heute":
        tg_send(cmd_heute(), chat_id)
    elif cmd == "/noten":
        parts = text.split(maxsplit=1)
        tg_send(cmd_noten(parts[1] if len(parts) > 1 else ""), chat_id)


def bot_loop():
    print("[telegram] Bot-Loop gestartet")
    offset = 0
    while True:
        res = tg_call("getUpdates", timeout=30, offset=offset)
        if res and res.get("ok"):
            for upd in res.get("result") or []:
                offset = max(offset, upd.get("update_id", 0) + 1)
                try:
                    handle_update(upd)
                except Exception as exc:
                    print("[telegram] handle:", exc)
        time.sleep(1)


def reminder_loop():
    print(f"[telegram] Erinnerung aktiv: {REMINDER_TIME} (Mo–Fr)")
    try:
        hh, mm = REMINDER_TIME.split(":")[:2]
        target = int(hh) * 60 + int(mm)
    except ValueError:
        print("[telegram] Ungültige REMINDER_TIME:", REMINDER_TIME)
        return
    fired = None
    while True:
        now = datetime.now()
        if now.weekday() < 5:
            key = now.strftime("%Y-%m-%d")
            if now.hour * 60 + now.minute >= target and fired != key:
                fired = key
                try:
                    s = get_state_raw()
                    wk = week_key_today()
                    lessons = ((s or {}).get("lessons") or {}).get(wk) or {}
                    d = now.weekday()
                    if not any(k.startswith(f"{d}-") for k in lessons):
                        tg_send(
                            f"⏰ <b>Erinnerung ({REMINDER_TIME})</b>\n"
                            f"Das Klassenbuch für heute ({DAYS[d]}) ist noch leer — jetzt eintragen!"
                        )
                except Exception as exc:
                    print("[telegram] reminder:", exc)
        time.sleep(30)


@app.on_event("startup")
def start_background_threads():
    if TELEGRAM_TOKEN:
        threading.Thread(target=bot_loop, daemon=True, name="tg-bot").start()
        if REMINDER_TIME and TELEGRAM_CHAT_ID:
            threading.Thread(target=reminder_loop, daemon=True, name="tg-reminder").start()
        if TELEGRAM_CHAT_ID:
            tg_send("✅ trt.Schulmanager-Bot ist verbunden.\n\n" + HELP_TEXT)


# Statisches Frontend — nach den API-Routen mounten
app.mount("/", StaticFiles(directory="static", html=True), name="static")

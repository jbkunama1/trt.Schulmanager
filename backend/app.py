"""trt.Schulmanager (LehrerWerk) — FastAPI-Backend mit SQLite, Login, Telegram und Ablage.

- Kompletter App-State als JSON-Dokument in SQLite (Tabelle `state`, einzelne Zeile)
- Login mit Passwort aus ENV (APP_PASSWORD), Session-Token im RAM
- Telegram-Integration (reine Standardbibliothek):
  * Bot-Commands: /start /help /status /heute /noten <Klasse> /files
  * Dateien/Fotos direkt in den Chat schicken -> landen in der Ablage
  * Taegliche Klassenbuch-Erinnerung (Mo-Fr) an TELEGRAM_CHAT_ID
- Ablage / Upload:
  * POST /api/upload (Rohbytes, optional UPLOAD_CODE fuer Schueler)
  * GET /api/files, GET/DELETE /api/files/{name} (Bearer-auth, Lehrer)
  * GET /upload -> eigenstaendige Upload-Seite (Schueler + Lehrer-Bereich)
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
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

DB_PATH = os.environ.get("DB_PATH", "/data/lehrerwerk.db")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "lehrer2026")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
REMINDER_TIME = os.environ.get("REMINDER_TIME", "").strip()  # z. B. "17:30", leer = aus
UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "/data/uploads")
UPLOAD_CODE = os.environ.get("UPLOAD_CODE", "").strip()  # leer = Upload ohne Code
MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MB (Telegram liefert max. 20 MB)

DAYS = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag"]

app = FastAPI(title="trt.Schulmanager API", version="2.2.0")
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


# ============ Ablage / Upload ============


def sanitize_filename(name: str) -> str:
    name = os.path.basename((name or "datei").replace("\\", "/"))
    out = []
    for ch in name:
        out.append(ch if (ch.isalnum() or ch in "-_. ") else "_")
    name = "".join(out).strip(" .") or "datei"
    return name[:120]


def unique_target(directory: Path, name: str) -> Path:
    candidate = directory / name
    stem, suffix = candidate.stem, candidate.suffix
    i = 1
    while candidate.exists():
        candidate = directory / f"{stem}_{i}{suffix}"
        i += 1
    return candidate


def store_upload(data: bytes, name: str) -> str:
    Path(UPLOAD_DIR).mkdir(parents=True, exist_ok=True)
    target = unique_target(Path(UPLOAD_DIR), sanitize_filename(name))
    target.write_bytes(data)
    return target.name


@app.post("/api/upload")
async def upload_file(request: Request, filename: str = "", code: str = ""):
    if UPLOAD_CODE and code != UPLOAD_CODE:
        raise HTTPException(status_code=401, detail="Falscher Zugangscode")
    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="Leere Datei")
    if len(body) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Datei zu gross (max. 25 MB)")
    name = store_upload(body, filename or "datei")
    return {"ok": True, "name": name, "size": len(body)}


@app.get("/api/files", dependencies=[Depends(require_auth)])
def list_files():
    d = Path(UPLOAD_DIR)
    files = []
    if d.exists():
        for p in sorted(d.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
            if p.is_file():
                st = p.stat()
                files.append(
                    {
                        "name": p.name,
                        "size": st.st_size,
                        "modified": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
                    }
                )
    return {"files": files}


@app.get("/api/files/{name}", dependencies=[Depends(require_auth)])
def download_file(name: str):
    safe = sanitize_filename(name)
    p = Path(UPLOAD_DIR) / safe
    if not p.is_file():
        raise HTTPException(status_code=404, detail="Datei nicht gefunden")
    return Response(
        content=p.read_bytes(),
        media_type="application/octet-stream",
        headers={"Content-Disposition": 'attachment; filename="' + safe + '"'},
    )


@app.delete("/api/files/{name}", dependencies=[Depends(require_auth)])
def delete_file(name: str):
    safe = sanitize_filename(name)
    p = Path(UPLOAD_DIR) / safe
    if p.is_file():
        p.unlink()
    return {"ok": True}


UPLOAD_PAGE = """<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>trt.Schulmanager — Ablage</title>
<style>
:root{--bg:#f4f6f9;--card:#fff;--ink:#16233a;--mut:#64748b;--acc:#2563eb;--line:#e2e8f0;--red:#dc2626}
*{box-sizing:border-box}
body{margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--ink)}
header{background:#101a2e;color:#fff;padding:14px 16px;font-weight:800;font-size:18px}
header span{font-weight:400;font-size:12px;color:#94a3b8;margin-left:8px}
main{max-width:760px;margin:0 auto;padding:20px 16px 60px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px;margin-bottom:16px}
h2{margin:0 0 4px;font-size:20px}
p{color:var(--mut);font-size:14px}
label{display:block;font-size:13px;color:var(--mut);margin:10px 0 4px;font-weight:600}
input{width:100%;padding:10px;border:1px solid var(--line);border-radius:8px;font-size:15px}
.btn{background:var(--acc);color:#fff;border:0;border-radius:8px;padding:10px 16px;font-size:15px;font-weight:600;cursor:pointer;margin-top:10px}
.btn.ghost{background:var(--card);color:var(--acc);border:1px solid var(--acc)}
.btn.red{background:var(--red)}
.msg{margin-top:10px;font-size:14px;padding:10px;border-radius:8px;display:none}
.show-ok{background:#dcfce7;color:#166534;display:block!important}
.show-err{background:#fee2e2;color:#991b1b;display:block!important}
table{border-collapse:collapse;width:100%;font-size:14px}
th,td{border:1px solid var(--line);padding:6px 9px;text-align:left}
th{background:#f1f5f9;font-size:12px;text-transform:uppercase;color:#475569}
.hidden{display:none}
</style>
</head>
<body>
<header>📎 trt.Schulmanager <span>Ablage / Upload</span></header>
<main>
<div class="card">
<h2>📤 Datei hochladen</h2>
<p>Dokument auswählen und hochladen — es landet direkt in der Ablage. Alternativ: einfach per Telegram an den Bot schicken.</p>
<label>Datei</label><input type="file" id="file">
<label>Zugangscode (falls erforderlich)</label><input type="text" id="code" placeholder="z. B. vom Lehrer erhalten" autocomplete="off">
<button class="btn" onclick="doUpload()">Hochladen</button>
<div id="umsg" class="msg"></div>
</div>
<div class="card">
<h2>🔐 Lehrer-Bereich</h2>
<p>Mit dem App-Passwort einloggen, um alle Dateien zu sehen, herunterzuladen oder zu löschen.</p>
<label>Passwort</label><input type="password" id="pw" placeholder="App-Passwort">
<button class="btn ghost" onclick="doLogin()">Anmelden</button>
<div id="lmsg" class="msg"></div>
<div id="filesWrap" class="hidden">
<div style="overflow-x:auto"><table id="ftable"><thead><tr><th>Datei</th><th>Größe</th><th>Datum</th><th style="width:100px"></th></tr></thead><tbody></tbody></table></div>
</div>
</div>
</main>
<script>
var TOKEN = sessionStorage.getItem("lw_token") || null;
function msg(id, text, cls){var el = document.getElementById(id); el.textContent = text; el.className = "msg " + cls;}
function fmtSize(b){return b > 1048576 ? (b / 1048576).toFixed(1) + " MB" : (b / 1024).toFixed(0) + " KB";}
async function doUpload(){
  var f = document.getElementById("file").files[0];
  if(!f){msg("umsg", "Bitte zuerst eine Datei auswählen.", "show-err"); return;}
  var code = document.getElementById("code").value;
  msg("umsg", "Lädt hoch …", "show-ok");
  try{
    var buf = await f.arrayBuffer();
    var r = await fetch("/api/upload?filename=" + encodeURIComponent(f.name) + "&code=" + encodeURIComponent(code), {method: "POST", body: buf});
    var j = await r.json().catch(function(){return {};});
    if(r.ok){msg("umsg", "✅ Gespeichert: " + (j.name || f.name), "show-ok"); if(TOKEN) loadFiles();}
    else msg("umsg", "❌ " + (j.detail || "Fehler"), "show-err");
  }catch(e){msg("umsg", "❌ Upload fehlgeschlagen: " + e.message, "show-err");}
}
async function doLogin(){
  try{
    var r = await fetch("/api/login", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({password: document.getElementById("pw").value})});
    if(!r.ok){msg("lmsg", "❌ Falsches Passwort", "show-err"); return;}
    var j = await r.json(); TOKEN = j.token; sessionStorage.setItem("lw_token", TOKEN);
    loadFiles();
  }catch(e){msg("lmsg", "❌ Server nicht erreichbar", "show-err");}
}
async function loadFiles(){
  try{
    var r = await fetch("/api/files", {headers: {Authorization: "Bearer " + TOKEN}});
    if(r.status === 401){TOKEN = null; sessionStorage.removeItem("lw_token"); msg("lmsg", "Sitzung abgelaufen — neu anmelden", "show-err"); return;}
    var j = await r.json();
    document.getElementById("filesWrap").classList.remove("hidden");
    var tb = document.querySelector("#ftable tbody"); tb.innerHTML = "";
    if(!j.files.length){tb.innerHTML = '<tr><td colspan="4" style="color:var(--mut)">Noch keine Dateien.</td></tr>'; return;}
    j.files.forEach(function(f){
      var tr = document.createElement("tr");
      var td = document.createElement("td"); td.textContent = f.name; tr.appendChild(td);
      td = document.createElement("td"); td.textContent = fmtSize(f.size); tr.appendChild(td);
      td = document.createElement("td"); td.textContent = new Date(f.modified).toLocaleString("de-DE"); tr.appendChild(td);
      td = document.createElement("td");
      var b1 = document.createElement("button"); b1.className = "btn ghost"; b1.style.cssText = "margin:0;padding:4px 10px;font-size:12px"; b1.textContent = "⬇"; b1.onclick = function(){dl(f.name);}; td.appendChild(b1);
      var b2 = document.createElement("button"); b2.className = "btn red"; b2.style.cssText = "margin:0 0 0 4px;padding:4px 10px;font-size:12px"; b2.textContent = "✕"; b2.onclick = function(){del(f.name);}; td.appendChild(b2);
      tr.appendChild(td);
      tb.appendChild(tr);
    });
  }catch(e){msg("lmsg", "❌ " + e.message, "show-err");}
}
async function dl(name){
  var r = await fetch("/api/files/" + encodeURIComponent(name), {headers: {Authorization: "Bearer " + TOKEN}});
  if(!r.ok) return;
  var blob = await r.blob();
  var a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = name; a.click();
  setTimeout(function(){URL.revokeObjectURL(a.href);}, 3000);
}
async function del(name){
  if(!confirm("Diese Datei wirklich löschen?")) return;
  await fetch("/api/files/" + encodeURIComponent(name), {method: "DELETE", headers: {Authorization: "Bearer " + TOKEN}});
  loadFiles();
}
if(TOKEN) loadFiles();
</script>
</body>
</html>
"""


@app.get("/upload")
def upload_page():
    return HTMLResponse(UPLOAD_PAGE)


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
    "/files — neueste Dateien in der Ablage\n"
    "/help — diese Übersicht\n\n"
    "📎 Dateien und Fotos einfach hier in den Chat schicken — "
    "sie landen automatisch in der Ablage (/upload)."
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


def save_telegram_file(doc: dict, chat_id: str):
    res = tg_call("getFile", file_id=doc.get("file_id"))
    if not res or not res.get("ok"):
        tg_send("❌ Datei konnte nicht abgerufen werden (max. 20 MB).", chat_id)
        return
    file_path = res["result"].get("file_path", "")
    url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url), timeout=90) as r:
            data = r.read()
    except Exception as exc:
        print("[telegram] download:", exc)
        tg_send("❌ Download fehlgeschlagen.", chat_id)
        return
    name = doc.get("file_name") or os.path.basename(file_path) or "datei"
    stored = store_upload(data, name)
    size = f"{len(data) / 1048576:.1f} MB" if len(data) > 1048576 else f"{len(data) / 1024:.0f} KB"
    tg_send(f"✅ <b>Gespeichert:</b> <code>{stored}</code> ({size})\n📍 Ablage: /upload", chat_id)


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


def cmd_files() -> str:
    d = Path(UPLOAD_DIR)
    if not d.exists() or not any(d.iterdir()):
        return "📎 Ablage ist leer — schick mir einfach Dateien, um sie abzulegen!"
    lines = ["📎 <b>Ablage — neueste Dateien</b>", ""]
    for p in sorted(d.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True)[:10]:
        if not p.is_file():
            continue
        st = p.stat()
        size = f"{st.st_size / 1048576:.1f} MB" if st.st_size > 1048576 else f"{st.st_size / 1024:.0f} KB"
        dt = datetime.fromtimestamp(st.st_mtime).strftime("%d.%m. %H:%M")
        lines.append(f"• {p.name} ({size}, {dt})")
    return "\n".join(lines)


def handle_update(upd: dict):
    msg = upd.get("message") or {}
    chat_id = str((msg.get("chat") or {}).get("id") or "")
    if not chat_id:
        return
    if TELEGRAM_CHAT_ID and chat_id != TELEGRAM_CHAT_ID:
        return
    text = (msg.get("text") or msg.get("caption") or "").strip()
    doc = msg.get("document")
    photo = msg.get("photo")
    if doc or photo:
        file_doc = doc or {
            "file_id": photo[-1]["file_id"],
            "file_name": f"foto_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg",
        }
        save_telegram_file(file_doc, chat_id)
        return
    if not text:
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
    elif cmd == "/files":
        tg_send(cmd_files(), chat_id)
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

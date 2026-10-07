"""Agenda di famiglia - server con promemoria push."""
import base64
import json
import mimetypes
import os
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

DATA_DIR = os.environ.get("DATA_DIR", "/data")
EVENTS_FILE = os.path.join(DATA_DIR, "events.json")
SUBS_FILE = os.path.join(DATA_DIR, "subscriptions.json")
SENT_FILE = os.path.join(DATA_DIR, "sent.json")
VAPID_FILE = os.path.join(DATA_DIR, "vapid_private.pem")
PEOPLE = [p.strip() for p in os.environ.get("PEOPLE", "Mamma,Papà").split(",") if p.strip()]
PORT = int(os.environ.get("PORT", "8080"))
TZ = ZoneInfo(os.environ.get("TZ", "Europe/Rome"))
VAPID_SUB = os.environ.get("VAPID_SUBJECT", "mailto:famiglia@example.com")
EVENING_HOUR = int(os.environ.get("REMINDER_EVENING_HOUR", "18"))   # promemoria "la sera prima"
MORNING_HOUR = int(os.environ.get("REMINDER_MORNING_HOUR", "8"))    # per eventi senza orario
HOURS_BEFORE = int(os.environ.get("REMINDER_HOURS_BEFORE", "2"))    # per eventi con orario
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

LOCK = threading.Lock()
FIELDS = {"cat": 30, "title": 120, "who": 40, "date": 10, "time": 5, "place": 200, "desc": 2000}
CAT_LABELS = {
    "visita": "Visita medica", "esami": "Esami e analisi", "farmaci": "Medicine",
    "pagamenti": "Pagamento", "banca": "Banca e Posta", "documenti": "Documenti",
    "famiglia": "Famiglia", "commissioni": "Commissioni", "compleanno": "Compleanno",
    "casa": "Casa e auto", "altro": "Appuntamento",
}
CAT_ICONS = {
    "visita": "🩺", "esami": "🧪", "farmaci": "💊", "pagamenti": "💶", "banca": "🏦",
    "documenti": "📄", "famiglia": "👨‍👩‍👧", "commissioni": "🛒", "compleanno": "🎂",
    "casa": "🔧", "altro": "📌",
}


# ---------- file JSON ----------
def read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def clean(payload):
    out = {}
    for key, limit in FIELDS.items():
        value = payload.get(key, "")
        out[key] = value.strip()[:limit] if isinstance(value, str) else ""
    out["done"] = bool(payload.get("done", False))
    return out


# ---------- chiavi VAPID ----------
def vapid_public_key():
    """Crea (la prima volta) la chiave VAPID e restituisce la parte pubblica in base64url."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    if not os.path.exists(VAPID_FILE):
        os.makedirs(DATA_DIR, exist_ok=True)
        key = ec.generate_private_key(ec.SECP256R1())
        with open(VAPID_FILE, "wb") as f:
            f.write(key.private_bytes(serialization.Encoding.PEM,
                                      serialization.PrivateFormat.PKCS8,
                                      serialization.NoEncryption()))
        os.chmod(VAPID_FILE, 0o600)
    with open(VAPID_FILE, "rb") as f:
        key = serialization.load_pem_private_key(f.read(), password=None)
    raw = key.public_key().public_bytes(serialization.Encoding.X962,
                                        serialization.PublicFormat.UncompressedPoint)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


# ---------- invio notifiche ----------
def send_push(sub, payload):
    """Invia una notifica a una sottoscrizione. Restituisce False se la sottoscrizione non esiste più."""
    from pywebpush import WebPushException, webpush
    try:
        webpush(subscription_info=sub["sub"], data=json.dumps(payload, ensure_ascii=False),
                vapid_private_key=VAPID_FILE, vapid_claims={"sub": VAPID_SUB}, ttl=6 * 3600)
        return True
    except WebPushException as exc:
        status = getattr(exc.response, "status_code", None)
        if status in (404, 410):
            return False
        print("Errore push:", exc, flush=True)
        return True
    except Exception as exc:  # rete assente, ecc.
        print("Errore push:", exc, flush=True)
        return True


def targets_for(event, subs):
    who = event.get("who") or "Tutti"
    return [s for s in subs if who == "Tutti" or s.get("who") == "Tutti" or s.get("who") == who]


def notify(event, title, body, kind, sender=send_push):
    with LOCK:
        subs = read_json(SUBS_FILE, [])
    dead = []
    for s in targets_for(event, subs):
        payload = {"title": title, "body": body, "tag": f"{event['id']}-{kind}"}
        if not sender(s, payload):
            dead.append(s["sub"]["endpoint"])
    if dead:
        with LOCK:
            subs = [s for s in read_json(SUBS_FILE, []) if s["sub"]["endpoint"] not in dead]
            write_json(SUBS_FILE, subs)


def when_text(event, now):
    d = datetime.strptime(event["date"], "%Y-%m-%d").date()
    if d == now.date():
        day = "oggi"
    elif d == (now + timedelta(days=1)).date():
        day = "domani"
    else:
        day = d.strftime("%d/%m")
    return day + (f" alle {event['time']}" if event.get("time") else "")


def due_reminders(event, now):
    """Elenco dei promemoria (tipo, istante, titolo, testo) previsti per un appuntamento."""
    try:
        day = datetime.strptime(event["date"], "%Y-%m-%d")
        if event.get("time"):
            hh, mm = map(int, event["time"].split(":"))
            start = day.replace(hour=hh, minute=mm, tzinfo=TZ)
        else:
            start = day.replace(hour=23, minute=59, tzinfo=TZ)
    except (ValueError, KeyError):
        return []
    icon = CAT_ICONS.get(event.get("cat"), "📌")
    name = event.get("title") or CAT_LABELS.get(event.get("cat"), "Appuntamento")
    evening = (day - timedelta(days=1)).replace(hour=EVENING_HOUR, minute=0, tzinfo=TZ)
    items = [("sera", evening, f"{icon} Domani: {name}",
              (f"Alle {event['time']}. " if event.get("time") else "") + (event.get("place") or ""))]
    if event.get("time"):
        items.append(("ore", start - timedelta(hours=HOURS_BEFORE), f"{icon} Tra {HOURS_BEFORE} ore: {name}",
                      f"Alle {event['time']}. " + (event.get("place") or "")))
    else:
        items.append(("mattina", day.replace(hour=MORNING_HOUR, tzinfo=TZ), f"{icon} Oggi: {name}",
                      event.get("place") or ""))
    return [(k, t, ti, body.strip()) for k, t, ti, body in items]


def check_reminders(now=None, sender=send_push):
    now = now or datetime.now(TZ)
    with LOCK:
        events = read_json(EVENTS_FILE, [])
        sent = read_json(SENT_FILE, {})
    changed = False
    for ev in events:
        if ev.get("done"):
            continue
        for kind, due, title, body in due_reminders(ev, now):
            key = f"{ev['id']}:{kind}:{ev['date']}:{ev.get('time','')}"
            # scatta solo se è ora (con 6 ore di tolleranza, per non inviare roba vecchia)
            if key in sent or not (due <= now < due + timedelta(hours=6)):
                continue
            sent[key] = now.isoformat()
            changed = True
            notify(ev, title, body, kind, sender)
    ids = {e["id"] for e in events}
    pruned = {k: v for k, v in sent.items() if k.split(":")[0] in ids}
    if changed or len(pruned) != len(sent):
        with LOCK:
            write_json(SENT_FILE, pruned)


def scheduler():
    while True:
        try:
            check_reminders()
        except Exception as exc:
            print("Errore promemoria:", exc, flush=True)
        time.sleep(30)


class Handler(BaseHTTPRequestHandler):
    server_version = "AgendaFamiglia"

    def log_message(self, fmt, *args):
        pass

    def send_json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_body(self):
        try:
            length = min(int(self.headers.get("Content-Length", "0")), 100_000)
            return json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return None

    # --- GET ---
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/config":
            return self.send_json(200, {"people": PEOPLE})
        if path == "/api/push/key":
            try:
                return self.send_json(200, {"key": vapid_public_key()})
            except Exception as exc:
                print("Errore chiave VAPID:", exc, flush=True)
                return self.send_json(500, {"error": "vapid"})
        if path == "/api/events":
            with LOCK:
                return self.send_json(200, read_json(EVENTS_FILE, []))
        self.serve_static(path)

    # --- POST ---
    def do_POST(self):
        path = urlparse(self.path).path
        if path not in ("/api/events", "/api/push/subscribe", "/api/push/unsubscribe"):
            return self.send_json(404, {"error": "not found"})
        payload = self.read_body()
        if payload is None:
            return self.send_json(400, {"error": "json"})

        if path == "/api/push/subscribe":
            sub = payload.get("subscription") or {}
            if not sub.get("endpoint") or not sub.get("keys"):
                return self.send_json(400, {"error": "subscription"})
            who = payload.get("who") if payload.get("who") in PEOPLE + ["Tutti"] else "Tutti"
            entry = {"sub": sub, "who": who}
            with LOCK:
                subs = [s for s in read_json(SUBS_FILE, []) if s["sub"]["endpoint"] != sub["endpoint"]]
                subs.append(entry)
                write_json(SUBS_FILE, subs)
            threading.Thread(target=lambda: send_push(entry, {
                "title": "🔔 Promemoria attivati", "body": "Da ora ti avviso io degli appuntamenti.",
                "tag": "benvenuto"}), daemon=True).start()
            return self.send_json(200, {"ok": True})

        if path == "/api/push/unsubscribe":
            endpoint = payload.get("endpoint")
            with LOCK:
                subs = [s for s in read_json(SUBS_FILE, []) if s["sub"]["endpoint"] != endpoint]
                write_json(SUBS_FILE, subs)
            return self.send_json(200, {"ok": True})

        event = clean(payload)
        if not event["date"]:
            return self.send_json(400, {"error": "date"})
        event["id"] = uuid.uuid4().hex[:12]
        with LOCK:
            events = read_json(EVENTS_FILE, [])
            events.append(event)
            write_json(EVENTS_FILE, events)
        # avviso subito chi deve saperlo
        icon = CAT_ICONS.get(event["cat"], "📌")
        title = f"{icon} Nuovo: {event['title'] or CAT_LABELS.get(event['cat'], 'Appuntamento')}"
        threading.Thread(target=notify, args=(event, title, when_text(event, datetime.now(TZ)), "nuovo"),
                         daemon=True).start()
        # i promemoria già "scaduti" per un evento appena creato non vanno inviati
        with LOCK:
            sent = read_json(SENT_FILE, {})
            now = datetime.now(TZ)
            for kind, due, _, _ in due_reminders(event, now):
                if due <= now:
                    sent[f"{event['id']}:{kind}:{event['date']}:{event.get('time','')}"] = now.isoformat()
            write_json(SENT_FILE, sent)
        self.send_json(201, event)

    # --- PUT / DELETE ---
    def do_PUT(self):
        parts = urlparse(self.path).path.strip("/").split("/")
        if len(parts) != 3 or parts[:2] != ["api", "events"]:
            return self.send_json(404, {"error": "not found"})
        payload = self.read_body()
        if payload is None:
            return self.send_json(400, {"error": "json"})
        with LOCK:
            events = read_json(EVENTS_FILE, [])
            for i, ev in enumerate(events):
                if ev["id"] == parts[2]:
                    updated = clean({**ev, **payload})
                    updated["id"] = ev["id"]
                    events[i] = updated
                    write_json(EVENTS_FILE, events)
                    return self.send_json(200, updated)
        self.send_json(404, {"error": "not found"})

    def do_DELETE(self):
        parts = urlparse(self.path).path.strip("/").split("/")
        if len(parts) != 3 or parts[:2] != ["api", "events"]:
            return self.send_json(404, {"error": "not found"})
        with LOCK:
            events = read_json(EVENTS_FILE, [])
            remaining = [e for e in events if e["id"] != parts[2]]
            write_json(EVENTS_FILE, remaining)
        self.send_json(200, {"deleted": len(events) - len(remaining)})

    # --- file statici ---
    def serve_static(self, path):
        if path == "/":
            path = "/index.html"
        full = os.path.realpath(os.path.join(STATIC, path.lstrip("/")))
        if not full.startswith(os.path.realpath(STATIC) + os.sep) or not os.path.isfile(full):
            self.send_response(404)
            self.end_headers()
            return
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if full.endswith(".webmanifest"):
            ctype = "application/manifest+json"
        with open(full, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text/") or "javascript" in ctype else ""))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    try:
        vapid_public_key()
    except Exception as exc:
        print("Attenzione: chiavi VAPID non create:", exc, flush=True)
    threading.Thread(target=scheduler, daemon=True).start()
    print(f"Agenda di famiglia in ascolto sulla porta {PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()

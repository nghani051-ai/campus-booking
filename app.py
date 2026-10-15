#!/usr/bin/env python3
"""
Smart Campus Resource & Room Booking System
-------------------------------------------
Self-contained app: Python standard library only (http.server + sqlite3).
Run:  python app.py   ->  opens http://localhost:8000
"""
import hashlib
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
import traceback
import webbrowser
from contextlib import contextmanager
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "campus.db")
INDEX = os.path.join(BASE, "static", "index.html")

OPEN_MIN, CLOSE_MIN, STEP = 8 * 60, 20 * 60, 30  # bookable window 08:00-20:00, 30-min slots
RESOURCE_TYPES = ["Room", "Hall", "Equipment", "Compute"]

lock = threading.Lock()
sessions = {}  # token -> user id


# ----------------------------------------------------------------- helpers
class ApiError(Exception):
    def __init__(self, code, msg, extra=None):
        super().__init__(msg)
        self.code, self.msg, self.extra = code, msg, extra or {}


@contextmanager
def tx():
    """One serialized DB transaction (the lock makes conflict-check + insert atomic)."""
    with lock:
        c = sqlite3.connect(DB_PATH, timeout=10)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()


def hash_pw(pw, salt):
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 60000).hex()


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def fmt(m):
    return f"{m // 60:02d}:{m % 60:02d}"


def parse_hm(s):
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", str(s or ""))
    if not m:
        raise ApiError(400, "Time must look like HH:MM")
    return int(m[1]) * 60 + int(m[2])


def parse_date(s):
    try:
        return datetime.strptime(str(s), "%Y-%m-%d").date()
    except ValueError:
        raise ApiError(400, "Date must look like YYYY-MM-DD")


def log(c, booking_id, actor, action, note=""):
    c.execute("INSERT INTO history(booking_id,actor,action,note,ts) VALUES(?,?,?,?,?)",
              (booking_id, actor, action, note, now_str()))


def notify(c, user_id, msg):
    c.execute("INSERT INTO notifications(user_id,message,is_read,ts) VALUES(?,?,0,?)",
              (user_id, msg, now_str()))


def booking_dict(r):
    d = dict(r)
    d["start"], d["end"] = fmt(d["start_min"]), fmt(d["end_min"])
    return d


# ---------------------------------------------------------------- database
def init_db():
    fresh = not os.path.exists(DB_PATH)
    with tx() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('student','admin')), salt TEXT NOT NULL, pw TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS resources(
            id INTEGER PRIMARY KEY, name TEXT NOT NULL, type TEXT NOT NULL, capacity INTEGER NOT NULL,
            location TEXT, specs TEXT, guidelines TEXT);
        CREATE TABLE IF NOT EXISTS bookings(
            id INTEGER PRIMARY KEY, resource_id INTEGER NOT NULL REFERENCES resources(id),
            user_id INTEGER NOT NULL REFERENCES users(id), date TEXT NOT NULL,
            start_min INTEGER NOT NULL, end_min INTEGER NOT NULL, purpose TEXT,
            status TEXT NOT NULL CHECK(status IN ('pending','approved','rejected','cancelled')),
            admin_note TEXT, created_at TEXT, updated_at TEXT);
        CREATE INDEX IF NOT EXISTS idx_b ON bookings(resource_id, date, status);
        CREATE TABLE IF NOT EXISTS history(
            id INTEGER PRIMARY KEY, booking_id INTEGER, actor TEXT, action TEXT, note TEXT, ts TEXT);
        CREATE TABLE IF NOT EXISTS notifications(
            id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, message TEXT, is_read INTEGER DEFAULT 0, ts TEXT);
        """)
        if fresh or not c.execute("SELECT 1 FROM users LIMIT 1").fetchone():
            seed(c)


def seed(c):
    for u, n, role, pw in [("admin", "Arya College of Engineering", "admin", "admin123"),
                           ("nouman", "Md. Nouman Ghani", "student", "pass123"),
                           ("diya", "Diya Nair", "student", "pass123")]:
        salt = secrets.token_hex(8)
        c.execute("INSERT INTO users(username,name,role,salt,pw) VALUES(?,?,?,?,?)",
                  (u, n, role, salt, hash_pw(pw, salt)))
    res = [
        ("Seminar Hall A", "Hall", 120, "Block A, Floor 1", "Projector, 4 wireless mics, AC, stage podium",
         "Book at least a day ahead. Food and drinks are not allowed. Leave the hall as you found it."),
        ("Main Auditorium", "Hall", 450, "Admin Block", "Surround sound, stage lighting, live-stream rig",
         "Events only. Faculty coordinator must be named in the purpose. Setup access 30 min before."),
        ("Study Room 1", "Room", 6, "Library, Floor 2", "Whiteboard, power outlets, TV screen",
         "Max 2 hours per booking. Keep noise low. Release the room if your group cancels."),
        ("Study Room 2", "Room", 10, "Library, Floor 2", "Whiteboard, large table, HDMI display",
         "Max 2 hours per booking. Keep noise low."),
        ("Computer Lab 3", "Room", 40, "CS Block, Floor 3", "40 workstations, Linux + Windows, projector",
         "Lab assistant must be present. No installing software without permission."),
        ("GPU Server Node 1", "Compute", 1, "Server Room, CS Block", "2x NVIDIA A5000, 128 GB RAM, Ubuntu 22.04",
         "Save checkpoints often. Jobs end automatically when the slot ends."),
        ("Arduino & IoT Kit", "Equipment", 1, "Electronics Lab store", "Arduino Mega, sensors pack, ESP32, jumper wires",
         "Collect and return at the lab store. You are responsible for lost parts."),
        ("Portable Projector", "Equipment", 1, "Media Desk, Admin Block", "1080p projector, HDMI cable, tripod screen",
         "Return to the Media Desk before closing time."),
    ]
    c.executemany("INSERT INTO resources(name,type,capacity,location,specs,guidelines) VALUES(?,?,?,?,?,?)", res)
    # a few demo bookings for tomorrow so the schedule and approvals are not empty
    t = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    demo = [(1, 3, t, 600, 720, "Technical club orientation", "approved", "Approved. Collect mics from the AV desk."),
            (6, 2, t, 840, 960, "Training CNN for mini project", "pending", None),
            (3, 3, t, 660, 780, "Exam revision group", "pending", None),
            (5, 3, t, 540, 660, "Web dev workshop", "approved", None)]
    for rid, uid, d, s, e, p, st, note in demo:
        cur = c.execute("INSERT INTO bookings(resource_id,user_id,date,start_min,end_min,purpose,status,admin_note,created_at,updated_at)"
                        " VALUES(?,?,?,?,?,?,?,?,?,?)", (rid, uid, d, s, e, p, st, note, now_str(), now_str()))
        log(c, cur.lastrowid, "System", "Created (demo data)")


# ----------------------------------------------------------------- handlers
def auth(h, admin=False):
    token = (h.headers.get("Authorization") or "").replace("Bearer ", "").strip()
    uid = sessions.get(token)
    if not uid:
        raise ApiError(401, "Please sign in")
    with tx() as c:
        u = c.execute("SELECT id,username,name,role FROM users WHERE id=?", (uid,)).fetchone()
    if not u:
        raise ApiError(401, "Please sign in")
    if admin and u["role"] != "admin":
        raise ApiError(403, "Admins only")
    return dict(u)


def login(d):
    with tx() as c:
        u = c.execute("SELECT * FROM users WHERE username=?", (str(d.get("username", "")).strip().lower(),)).fetchone()
    if not u or not secrets.compare_digest(u["pw"], hash_pw(str(d.get("password", "")), u["salt"])):
        raise ApiError(401, "Wrong username or password")
    token = secrets.token_hex(24)
    sessions[token] = u["id"]
    return {"token": token, "user": {"id": u["id"], "username": u["username"], "name": u["name"], "role": u["role"]}}


def register(d):
    username = str(d.get("username", "")).strip().lower()
    name = str(d.get("name", "")).strip()
    pw = str(d.get("password", ""))
    if not re.fullmatch(r"[a-z0-9_.]{3,20}", username):
        raise ApiError(400, "Username: 3-20 letters, numbers, dot or underscore")
    if len(name) < 2:
        raise ApiError(400, "Please enter your full name")
    if len(pw) < 4:
        raise ApiError(400, "Password needs at least 4 characters")
    salt = secrets.token_hex(8)
    try:
        with tx() as c:
            c.execute("INSERT INTO users(username,name,role,salt,pw) VALUES(?,?,?,?,?)",
                      (username, name, "student", salt, hash_pw(pw, salt)))
    except sqlite3.IntegrityError:
        raise ApiError(409, "That username is taken")
    return login({"username": username, "password": pw})


def list_resources(q):
    text = (q.get("q") or "").lower()
    rtype = q.get("type") or ""
    today = datetime.now().strftime("%Y-%m-%d")
    with tx() as c:
        rows = c.execute("""SELECT r.*, (SELECT COUNT(*) FROM bookings b WHERE b.resource_id=r.id AND b.date=?
                            AND b.status IN ('pending','approved')) AS today_count FROM resources r ORDER BY r.type, r.name""",
                         (today,)).fetchall()
    out = [dict(r) for r in rows]
    if rtype and rtype != "All":
        out = [r for r in out if r["type"] == rtype]
    if text:
        out = [r for r in out if text in " ".join(str(r[k]) for k in ("name", "type", "specs", "location")).lower()]
    return out


def add_resource(d):
    name = str(d.get("name", "")).strip()
    rtype = d.get("type")
    try:
        cap = int(d.get("capacity"))
    except (TypeError, ValueError):
        raise ApiError(400, "Capacity must be a number")
    if len(name) < 2:
        raise ApiError(400, "Give the resource a name")
    if rtype not in RESOURCE_TYPES:
        raise ApiError(400, "Pick a valid type")
    if cap < 1:
        raise ApiError(400, "Capacity must be at least 1")
    with tx() as c:
        cur = c.execute("INSERT INTO resources(name,type,capacity,location,specs,guidelines) VALUES(?,?,?,?,?,?)",
                        (name, rtype, cap, str(d.get("location", "")).strip(), str(d.get("specs", "")).strip(),
                         str(d.get("guidelines", "")).strip()))
    return {"id": cur.lastrowid}


def schedule(u, q):
    rid, date = int(q.get("resource_id", 0)), q.get("date", "")
    parse_date(date)
    with tx() as c:
        rows = c.execute("""SELECT b.id,b.start_min,b.end_min,b.status,b.purpose,b.user_id,u.name AS user_name
                            FROM bookings b JOIN users u ON u.id=b.user_id
                            WHERE b.resource_id=? AND b.date=? AND b.status IN ('pending','approved')
                            ORDER BY b.start_min""", (rid, date)).fetchall()
    out = []
    for r in rows:
        d = booking_dict(r)
        d["mine"] = r["user_id"] == u["id"]
        if not (d["mine"] or u["role"] == "admin"):
            d["purpose"] = None  # privacy: only owner/admin see the purpose
        out.append(d)
    return out


def suggest_slots(c, rid, date_s, start, end, limit=3):
    """Free windows of the same length on the same day, nearest to the requested start first."""
    dur = end - start
    now = datetime.now()
    earliest = OPEN_MIN
    if date_s == str(now.date()):
        earliest = max(OPEN_MIN, (now.hour * 60 + now.minute + STEP - 1) // STEP * STEP)
    taken = c.execute("SELECT start_min,end_min FROM bookings WHERE resource_id=? AND date=? AND status IN ('pending','approved')",
                      (rid, date_s)).fetchall()
    found = []
    for s0 in range(earliest, CLOSE_MIN - dur + 1, STEP):
        if not any(s0 < t["end_min"] and s0 + dur > t["start_min"] for t in taken):
            found.append(s0)
    found.sort(key=lambda x: abs(x - start))
    return [{"start": fmt(x), "end": fmt(x + dur)} for x in sorted(found[:limit])]


def create_booking(u, d):
    try:
        rid = int(d.get("resource_id"))
    except (TypeError, ValueError):
        raise ApiError(400, "Choose a resource")
    day = parse_date(d.get("date"))
    start, end = parse_hm(d.get("start")), parse_hm(d.get("end"))
    purpose = str(d.get("purpose", "")).strip()
    try:
        repeat = int(d.get("repeat") or 1)
    except (TypeError, ValueError):
        raise ApiError(400, "Repeat must be a number of weeks")
    if not 1 <= repeat <= 8:
        raise ApiError(400, "You can repeat a booking for 1 to 8 weeks")
    if len(purpose) < 3:
        raise ApiError(400, "Add a short purpose (at least 3 characters)")
    if start % STEP or end % STEP:
        raise ApiError(400, "Times must be on the half hour")
    if start < OPEN_MIN or end > CLOSE_MIN or end <= start:
        raise ApiError(400, f"Bookings run between {fmt(OPEN_MIN)} and {fmt(CLOSE_MIN)}, and the end must be after the start")
    now = datetime.now()
    if day < now.date() or (day == now.date() and start < now.hour * 60 + now.minute):
        raise ApiError(400, "That time has already passed")
    days = [day + timedelta(weeks=i) for i in range(repeat)]
    with tx() as c:
        res = c.execute("SELECT * FROM resources WHERE id=?", (rid,)).fetchone()
        if not res:
            raise ApiError(404, "Resource not found")
        for dd in days:  # all-or-nothing: any clash rejects the whole series
            clash = c.execute("""SELECT b.start_min,b.end_min,b.status FROM bookings b
                                 WHERE b.resource_id=? AND b.date=? AND b.status IN ('pending','approved')
                                 AND ? < b.end_min AND ? > b.start_min LIMIT 1""", (rid, str(dd), start, end)).fetchone()
            if clash:
                where = f" on {dd}" if repeat > 1 else ""
                sug = suggest_slots(c, rid, str(dd), start, end) if repeat == 1 else []
                raise ApiError(409, f"Clash{where}: {res['name']} is already {clash['status']} from {fmt(clash['start_min'])} to {fmt(clash['end_min'])}. "
                                    + ("Try one of the free slots below." if sug else "Pick another slot."), {"suggestions": sug})
        status = "approved" if u["role"] == "admin" else "pending"
        ids = []
        for i, dd in enumerate(days):
            cur = c.execute("INSERT INTO bookings(resource_id,user_id,date,start_min,end_min,purpose,status,admin_note,created_at,updated_at)"
                            " VALUES(?,?,?,?,?,?,?,?,?,?)", (rid, u["id"], str(dd), start, end, purpose, status,
                                                             "Auto-approved (admin booking)" if status == "approved" else None,
                                                             now_str(), now_str()))
            ids.append(cur.lastrowid)
            series = f" (week {i + 1} of {repeat})" if repeat > 1 else ""
            log(c, cur.lastrowid, u["name"], "Requested" if status == "pending" else "Booked (auto-approved)",
                f"{res['name']} on {dd} {fmt(start)}-{fmt(end)}{series}")
        if status == "pending":
            extra = f" every week for {repeat} weeks" if repeat > 1 else ""
            for a in c.execute("SELECT id FROM users WHERE role='admin'").fetchall():
                notify(c, a["id"], f"New request: {u['name']} wants {res['name']} on {day}, {fmt(start)}-{fmt(end)}{extra}")
    return {"id": ids[0], "ids": ids, "count": len(ids), "status": status}


def expire_pending():
    """Pending requests whose start time has passed are closed automatically."""
    now = datetime.now()
    today, nm = str(now.date()), now.hour * 60 + now.minute
    with tx() as c:
        rows = c.execute(BOOKING_SELECT + "WHERE b.status='pending' AND (b.date<? OR (b.date=? AND b.start_min<=?))",
                         (today, today, nm)).fetchall()
        for b in rows:
            c.execute("UPDATE bookings SET status='cancelled', admin_note=?, updated_at=? WHERE id=?",
                      ("Expired: no decision before the start time", now_str(), b["id"]))
            log(c, b["id"], "System", "Expired", f"{b['resource_name']} on {b['date']} {fmt(b['start_min'])}-{fmt(b['end_min'])}")
            notify(c, b["user_id"], f"Your request for {b['resource_name']} on {b['date']} expired because no decision was made in time")


BOOKING_SELECT = """SELECT b.*, r.name AS resource_name, r.type AS resource_type, u.name AS user_name
                    FROM bookings b JOIN resources r ON r.id=b.resource_id JOIN users u ON u.id=b.user_id """


def my_bookings(u):
    with tx() as c:
        rows = c.execute(BOOKING_SELECT + "WHERE b.user_id=? ORDER BY b.date DESC, b.start_min DESC", (u["id"],)).fetchall()
    return [booking_dict(r) for r in rows]


def cancel_booking(u, bid):
    with tx() as c:
        b = c.execute(BOOKING_SELECT + "WHERE b.id=?", (bid,)).fetchone()
        if not b:
            raise ApiError(404, "Booking not found")
        if b["user_id"] != u["id"] and u["role"] != "admin":
            raise ApiError(403, "You can only cancel your own bookings")
        if b["status"] not in ("pending", "approved"):
            raise ApiError(400, f"A {b['status']} booking can't be cancelled")
        c.execute("UPDATE bookings SET status='cancelled', updated_at=? WHERE id=?", (now_str(), bid))
        log(c, bid, u["name"], "Cancelled", f"{b['resource_name']} on {b['date']} {fmt(b['start_min'])}-{fmt(b['end_min'])}")
        if b["user_id"] != u["id"]:
            notify(c, b["user_id"], f"Your booking for {b['resource_name']} on {b['date']} was cancelled by an admin")
    return {"ok": True}


def admin_bookings(q):
    status = q.get("status", "")
    sql = BOOKING_SELECT + ("WHERE b.status=? " if status else "WHERE 1=1 ")
    sql += "ORDER BY b.date ASC, b.start_min ASC" if status == "pending" else "ORDER BY b.updated_at DESC LIMIT 100"
    with tx() as c:
        rows = c.execute(sql, (status,) if status else ()).fetchall()
    return [booking_dict(r) for r in rows]


def decide(u, bid, d):
    decision, note = d.get("decision"), str(d.get("note", "")).strip()
    if decision not in ("approved", "rejected"):
        raise ApiError(400, "Decision must be approved or rejected")
    if decision == "rejected" and len(note) < 3:
        raise ApiError(400, "Add a short note explaining the rejection")
    with tx() as c:
        b = c.execute(BOOKING_SELECT + "WHERE b.id=?", (bid,)).fetchone()
        if not b:
            raise ApiError(404, "Booking not found")
        if b["status"] != "pending":
            raise ApiError(409, f"This request is already {b['status']}")
        c.execute("UPDATE bookings SET status=?, admin_note=?, updated_at=? WHERE id=?", (decision, note or None, now_str(), bid))
        log(c, bid, u["name"], decision.capitalize(), note)
        notify(c, b["user_id"], f"Your request for {b['resource_name']} on {b['date']} ({fmt(b['start_min'])}-{fmt(b['end_min'])}) was {decision}"
                                + (f": {note}" if note else ""))
    return {"ok": True}


def history():
    with tx() as c:
        rows = c.execute("""SELECT h.*, r.name AS resource_name FROM history h
                            LEFT JOIN bookings b ON b.id=h.booking_id LEFT JOIN resources r ON r.id=b.resource_id
                            ORDER BY h.id DESC LIMIT 200""").fetchall()
    return [dict(r) for r in rows]


def stats():
    with tx() as c:
        counts = {r["status"]: r["n"] for r in c.execute("SELECT status, COUNT(*) n FROM bookings GROUP BY status")}
        usage = c.execute("""SELECT r.name, COALESCE(SUM(CASE WHEN b.status='approved' THEN b.end_min-b.start_min END),0)/60.0 AS hours
                             FROM resources r LEFT JOIN bookings b ON b.resource_id=r.id GROUP BY r.id ORDER BY hours DESC""").fetchall()
        n_res = c.execute("SELECT COUNT(*) FROM resources").fetchone()[0]
    return {"counts": counts, "resources": n_res, "usage": [{"name": r["name"], "hours": round(r["hours"], 1)} for r in usage]}


def notifications(u):
    with tx() as c:
        rows = c.execute("SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 30", (u["id"],)).fetchall()
    items = [dict(r) for r in rows]
    return {"items": items, "unread": sum(1 for r in items if not r["is_read"])}


def dispatch(h, method, path, q, d):
    m = None
    if path not in ("/login", "/register", "/logout"):
        expire_pending()
    if method == "POST" and path == "/login":
        return login(d)
    if method == "POST" and path == "/register":
        return register(d)
    if method == "POST" and path == "/logout":
        sessions.pop((h.headers.get("Authorization") or "").replace("Bearer ", "").strip(), None)
        return {"ok": True}
    if method == "GET" and path == "/me":
        return auth(h)
    if method == "GET" and path == "/resources":
        auth(h)
        return list_resources(q)
    if method == "POST" and path == "/resources":
        auth(h, admin=True)
        return add_resource(d)
    if method == "GET" and path == "/schedule":
        return schedule(auth(h), q)
    if method == "POST" and path == "/bookings":
        return create_booking(auth(h), d)
    if method == "GET" and path == "/bookings/mine":
        return my_bookings(auth(h))
    if method == "POST" and (m := re.fullmatch(r"/bookings/(\d+)/cancel", path)):
        return cancel_booking(auth(h), int(m[1]))
    if method == "GET" and path == "/admin/bookings":
        auth(h, admin=True)
        return admin_bookings(q)
    if method == "POST" and (m := re.fullmatch(r"/admin/bookings/(\d+)/decision", path)):
        return decide(auth(h, admin=True), int(m[1]), d)
    if method == "GET" and path == "/admin/history":
        auth(h, admin=True)
        return history()
    if method == "GET" and path == "/admin/stats":
        auth(h, admin=True)
        return stats()
    if method == "GET" and path == "/notifications":
        return notifications(auth(h))
    if method == "POST" and path == "/notifications/read":
        u = auth(h)
        with tx() as c:
            c.execute("UPDATE notifications SET is_read=1 WHERE user_id=?", (u["id"],))
        return {"ok": True}
    raise ApiError(404, "Not found")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def handle_any(self, method):
        try:
            url = urlparse(self.path)
            if not url.path.startswith("/api/"):
                if url.path in ("/", "/index.html"):
                    with open(INDEX, "rb") as f:
                        body = f.read()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_json({"error": "Not found"}, 404)
                return
            data = {}
            if method == "POST":
                n = int(self.headers.get("Content-Length") or 0)
                if n:
                    try:
                        data = json.loads(self.rfile.read(n))
                    except ValueError:
                        raise ApiError(400, "Invalid JSON")
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            self.send_json(dispatch(self, method, url.path[4:], q, data))
        except ApiError as e:
            self.send_json({"error": e.msg, **e.extra}, e.code)
        except Exception:
            traceback.print_exc()
            self.send_json({"error": "Something went wrong on the server"}, 500)

    def do_GET(self):
        self.handle_any("GET")

    def do_POST(self):
        self.handle_any("POST")


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("PORT", 8000))
    init_db()
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://localhost:{port}"
    print(f"\n  Campus Booking running at {url}\n  Demo logins:  admin / admin123   |   nouman / pass123   |   diya / pass123\n  Ctrl+C to stop\n")
    if "--no-browser" not in sys.argv:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()

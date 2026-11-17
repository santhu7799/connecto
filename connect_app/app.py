"""
Connect – an online friendship app built with Python + Flask + SQLite.

Features
  * Sign up / log in (passwords hashed, 18+ only)
  * Profiles with photo, bio, city and interests
  * Discover people ranked by shared interests and city
  * Friend requests (send / accept / decline / unfriend)
  * 1-to-1 chat between friends (auto-refreshing, with read receipts)
  * Voice & video calls between friends, started from the chat header (WebRTC)
  * Block users (they disappear from each other's app)
  * Live online/offline status (Socket.IO presence)
  * RandomCall – random 1-to-1 voice calls between online users (WebRTC)

Run:
    pip install -r requirements.txt
    python app.py --seed      # optional: add demo users
    python app.py             # open http://127.0.0.1:5000
"""
import os
import secrets
import sqlite3
import sys
import threading
from datetime import datetime
from functools import wraps

from flask import (Flask, abort, flash, g, jsonify, redirect, render_template,
                   request, session, url_for)
from flask_socketio import SocketIO, emit, join_room
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("CONNECT_DB", os.path.join(BASE_DIR, "connect.db"))
UPLOAD_DIR = os.path.join(BASE_DIR, "static", "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-change-me-in-production")
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024  # photo uploads
socketio = SocketIO(app)  # threading mode: works with the plain dev server

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    display_name  TEXT NOT NULL,
    age           INTEGER NOT NULL,
    city          TEXT DEFAULT '',
    bio           TEXT DEFAULT '',
    interests     TEXT DEFAULT '',
    photo         TEXT DEFAULT '',
    random_calls  INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS friendships (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    requester_id INTEGER NOT NULL REFERENCES users(id),
    addressee_id INTEGER NOT NULL REFERENCES users(id),
    status       TEXT NOT NULL CHECK (status IN ('pending', 'accepted')),
    created_at   TEXT NOT NULL,
    UNIQUE (requester_id, addressee_id)
);
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    sender_id   INTEGER NOT NULL REFERENCES users(id),
    receiver_id INTEGER NOT NULL REFERENCES users(id),
    body        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    is_read     INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS blocks (
    blocker_id INTEGER NOT NULL REFERENCES users(id),
    blocked_id INTEGER NOT NULL REFERENCES users(id),
    PRIMARY KEY (blocker_id, blocked_id)
);
CREATE INDEX IF NOT EXISTS idx_msg_pair ON messages(sender_id, receiver_id, id);
"""


# --------------------------------------------------------------------------- #
# Database helpers
# --------------------------------------------------------------------------- #
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    with sqlite3.connect(DB_PATH) as db:
        db.executescript(SCHEMA)
        # migrate databases created before photos / random calls existed
        cols = {row[1] for row in db.execute("PRAGMA table_info(users)")}
        if "photo" not in cols:
            db.execute("ALTER TABLE users ADD COLUMN photo TEXT DEFAULT ''")
        if "random_calls" not in cols:
            db.execute("ALTER TABLE users ADD COLUMN random_calls INTEGER NOT NULL DEFAULT 1")


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def parse_interests(text):
    """'Music, hiking ,  Coding' -> {'music', 'hiking', 'coding'}"""
    return {t.strip().lower() for t in (text or "").split(",") if t.strip()}


# (magic bytes, extension) pairs for allowed profile photo formats
_IMAGE_SIGNATURES = [
    (b"\xff\xd8\xff", "jpg"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
]


def save_photo(file_storage, user):
    """Validate an uploaded image and store it. Returns filename or None."""
    head = file_storage.read(16)
    file_storage.seek(0)
    ext = None
    for sig, e in _IMAGE_SIGNATURES:
        if head.startswith(sig):
            ext = e
            break
    if ext is None and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        ext = "webp"
    if ext is None:
        return None
    filename = f"u{user['id']}_{secrets.token_hex(6)}.{ext}"
    file_storage.save(os.path.join(UPLOAD_DIR, filename))
    delete_photo(user)  # remove the old file after the new one is safely saved
    return filename


def delete_photo(user):
    if user["photo"]:
        try:
            os.remove(os.path.join(UPLOAD_DIR, user["photo"]))
        except OSError:
            pass


def friendship_between(a, b):
    return get_db().execute(
        """SELECT * FROM friendships
           WHERE (requester_id = ? AND addressee_id = ?)
              OR (requester_id = ? AND addressee_id = ?)""",
        (a, b, b, a),
    ).fetchone()


def friend_status(me, other):
    """Returns 'friends', 'sent', 'received' or None."""
    f = friendship_between(me, other)
    if f is None:
        return None
    if f["status"] == "accepted":
        return "friends"
    return "sent" if f["requester_id"] == me else "received"


def is_blocked(a, b):
    return get_db().execute(
        """SELECT 1 FROM blocks
           WHERE (blocker_id = ? AND blocked_id = ?)
              OR (blocker_id = ? AND blocked_id = ?)""",
        (a, b, b, a),
    ).fetchone() is not None


def get_user_or_404(user_id):
    user = get_db().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if user is None or (g.user and is_blocked(g.user["id"], user_id)):
        abort(404)
    return user


def safe_next(default):
    nxt = request.form.get("next") or request.args.get("next") or ""
    if nxt.startswith("/") and not nxt.startswith("//"):
        return nxt
    return default


# --------------------------------------------------------------------------- #
# Auth, CSRF, template context
# --------------------------------------------------------------------------- #
@app.before_request
def load_user_and_check_csrf():
    g.user = None
    uid = session.get("user_id")
    if uid:
        g.user = get_db().execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
        if g.user is None:
            session.clear()

    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    if request.method == "POST":
        token = request.form.get("csrf") or request.headers.get("X-CSRF-Token") or ""
        if not secrets.compare_digest(token, session["csrf"]):
            abort(400, "Invalid or missing CSRF token")


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


@app.context_processor
def inject_globals():
    ctx = {"csrf": session.get("csrf", ""), "pending_count": 0, "unread_count": 0,
           "is_online": lambda uid: uid in ONLINE}
    if g.get("user"):
        db = get_db()
        ctx["pending_count"] = db.execute(
            "SELECT COUNT(*) FROM friendships WHERE addressee_id = ? AND status = 'pending'",
            (g.user["id"],),
        ).fetchone()[0]
        ctx["unread_count"] = db.execute(
            "SELECT COUNT(*) FROM messages WHERE receiver_id = ? AND is_read = 0",
            (g.user["id"],),
        ).fetchone()[0]
    return ctx


@app.template_filter("interests")
def interests_filter(text):
    return sorted(parse_interests(text))


# --------------------------------------------------------------------------- #
# Pages: landing, register, login
# --------------------------------------------------------------------------- #
@app.route("/")
def index():
    if g.user:
        return redirect(url_for("discover"))
    return render_template("index.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if g.user:
        return redirect(url_for("discover"))
    form = request.form
    if request.method == "POST":
        username = form.get("username", "").strip().lower()
        password = form.get("password", "")
        display_name = form.get("display_name", "").strip()
        city = form.get("city", "").strip()
        interests = ", ".join(sorted(parse_interests(form.get("interests", ""))))
        try:
            age = int(form.get("age", "0"))
        except ValueError:
            age = 0

        error = None
        if not username.isalnum() or not 3 <= len(username) <= 20:
            error = "Username must be 3–20 letters or numbers."
        elif len(password) < 8:
            error = "Password must be at least 8 characters."
        elif not display_name:
            error = "Please enter your name."
        elif age < 18 or age > 120:
            error = "You must be 18 or older to use Connect."
        elif get_db().execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
            error = "That username is taken."

        if error:
            flash(error, "error")
        else:
            db = get_db()
            cur = db.execute(
                """INSERT INTO users (username, password_hash, display_name, age, city,
                                      interests, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (username, generate_password_hash(password), display_name, age, city,
                 interests, now()),
            )
            db.commit()
            session.clear()
            session["user_id"] = cur.lastrowid
            flash(f"Welcome to Connect, {display_name}! Add a bio so people get to know you.", "ok")
            return redirect(url_for("edit_profile"))
    return render_template("register.html", form=form)


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")
        user = get_db().execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if user and check_password_hash(user["password_hash"], password):
            session.clear()
            session["user_id"] = user["id"]
            return redirect(safe_next(url_for("discover")))
        flash("Wrong username or password.", "error")
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("index"))


# --------------------------------------------------------------------------- #
# Discover & profiles
# --------------------------------------------------------------------------- #
@app.route("/discover")
@login_required
def discover():
    me = g.user
    my_interests = parse_interests(me["interests"])
    my_city = (me["city"] or "").strip().lower()
    q = request.args.get("q", "").strip().lower()
    city = request.args.get("city", "").strip().lower()

    rows = get_db().execute(
        """SELECT * FROM users u
           WHERE u.id != ?
             AND NOT EXISTS (SELECT 1 FROM blocks b
                             WHERE (b.blocker_id = ? AND b.blocked_id = u.id)
                                OR (b.blocker_id = u.id AND b.blocked_id = ?))""",
        (me["id"], me["id"], me["id"]),
    ).fetchall()

    people = []
    for u in rows:
        ints = parse_interests(u["interests"])
        u_city = (u["city"] or "").strip().lower()
        if q and q not in u["display_name"].lower() and not any(q in i for i in ints):
            continue
        if city and city not in u_city:
            continue
        shared = my_interests & ints
        same_city = bool(my_city) and u_city == my_city
        union = my_interests | ints
        match = round(100 * len(shared) / len(union)) if union else 0
        people.append({
            "user": u,
            "shared": sorted(shared),
            "others": sorted(ints - shared),
            "same_city": same_city,
            "match": match,
            "score": len(shared) * 10 + (5 if same_city else 0),
            "status": friend_status(me["id"], u["id"]),
        })
    people.sort(key=lambda p: (-p["score"], p["user"]["display_name"].lower()))
    return render_template("discover.html", people=people, q=q, city=city)


@app.route("/u/<int:user_id>")
@login_required
def view_user(user_id):
    if user_id == g.user["id"]:
        return redirect(url_for("edit_profile"))
    user = get_user_or_404(user_id)
    shared = parse_interests(g.user["interests"]) & parse_interests(user["interests"])
    return render_template("user.html", user=user, shared=shared,
                           status=friend_status(g.user["id"], user_id))


@app.route("/profile", methods=["GET", "POST"])
@login_required
def edit_profile():
    if request.method == "POST":
        display_name = request.form.get("display_name", "").strip()
        if not display_name:
            flash("Name can't be empty.", "error")
        else:
            photo = g.user["photo"]
            upload = request.files.get("photo")
            if request.form.get("remove_photo"):
                delete_photo(g.user)
                photo = ""
            elif upload and upload.filename:
                saved = save_photo(upload, g.user)
                if saved is None:
                    flash("Photo must be a JPG, PNG, GIF or WebP image.", "error")
                    return redirect(url_for("edit_profile"))
                photo = saved
            db = get_db()
            db.execute(
                """UPDATE users SET display_name = ?, city = ?, bio = ?, interests = ?,
                                    photo = ?, random_calls = ? WHERE id = ?""",
                (display_name,
                 request.form.get("city", "").strip()[:60],
                 request.form.get("bio", "").strip()[:500],
                 ", ".join(sorted(parse_interests(request.form.get("interests", ""))))[:300],
                 photo,
                 1 if request.form.get("random_calls") else 0,
                 g.user["id"]),
            )
            db.commit()
            flash("Profile saved.", "ok")
            return redirect(url_for("edit_profile"))
    return render_template("profile.html")


# --------------------------------------------------------------------------- #
# Friends
# --------------------------------------------------------------------------- #
@app.route("/friends")
@login_required
def friends():
    me = g.user["id"]
    db = get_db()
    incoming = db.execute(
        """SELECT u.* FROM friendships f JOIN users u ON u.id = f.requester_id
           WHERE f.addressee_id = ? AND f.status = 'pending' ORDER BY f.created_at DESC""",
        (me,),
    ).fetchall()
    outgoing = db.execute(
        """SELECT u.* FROM friendships f JOIN users u ON u.id = f.addressee_id
           WHERE f.requester_id = ? AND f.status = 'pending' ORDER BY f.created_at DESC""",
        (me,),
    ).fetchall()
    friend_rows = db.execute(
        """SELECT u.*,
             (SELECT COUNT(*) FROM messages m
               WHERE m.sender_id = u.id AND m.receiver_id = ? AND m.is_read = 0) AS unread,
             (SELECT body FROM messages m
               WHERE (m.sender_id = u.id AND m.receiver_id = ?)
                  OR (m.sender_id = ? AND m.receiver_id = u.id)
               ORDER BY m.id DESC LIMIT 1) AS last_msg,
             (SELECT MAX(m.id) FROM messages m
               WHERE (m.sender_id = u.id AND m.receiver_id = ?)
                  OR (m.sender_id = ? AND m.receiver_id = u.id)) AS last_id
           FROM friendships f
           JOIN users u ON u.id = CASE WHEN f.requester_id = ? THEN f.addressee_id
                                       ELSE f.requester_id END
           WHERE (f.requester_id = ? OR f.addressee_id = ?) AND f.status = 'accepted'
           ORDER BY last_id IS NULL, last_id DESC, u.display_name""",
        (me, me, me, me, me, me, me, me),
    ).fetchall()
    return render_template("friends.html", incoming=incoming, outgoing=outgoing,
                           friends=friend_rows)


@app.route("/friend/request/<int:user_id>", methods=["POST"])
@login_required
def send_request(user_id):
    me = g.user["id"]
    if user_id == me:
        abort(400)
    get_user_or_404(user_id)
    status = friend_status(me, user_id)
    db = get_db()
    if status is None:
        db.execute(
            "INSERT INTO friendships (requester_id, addressee_id, status, created_at) "
            "VALUES (?, ?, 'pending', ?)", (me, user_id, now()))
        flash("Friend request sent!", "ok")
    elif status == "received":  # they already asked us -> just accept
        db.execute(
            "UPDATE friendships SET status = 'accepted' WHERE requester_id = ? AND addressee_id = ?",
            (user_id, me))
        flash("You're now friends!", "ok")
    db.commit()
    return redirect(safe_next(url_for("discover")))


@app.route("/friend/accept/<int:user_id>", methods=["POST"])
@login_required
def accept_request(user_id):
    db = get_db()
    cur = db.execute(
        """UPDATE friendships SET status = 'accepted'
           WHERE requester_id = ? AND addressee_id = ? AND status = 'pending'""",
        (user_id, g.user["id"]))
    db.commit()
    if cur.rowcount:
        flash("You're now friends! Say hi 👋", "ok")
    return redirect(safe_next(url_for("friends")))


@app.route("/friend/remove/<int:user_id>", methods=["POST"])
@login_required
def remove_friend(user_id):
    """Decline an incoming request, cancel an outgoing one, or unfriend."""
    me = g.user["id"]
    db = get_db()
    db.execute(
        """DELETE FROM friendships
           WHERE (requester_id = ? AND addressee_id = ?)
              OR (requester_id = ? AND addressee_id = ?)""",
        (me, user_id, user_id, me))
    db.commit()
    return redirect(safe_next(url_for("friends")))


@app.route("/block/<int:user_id>", methods=["POST"])
@login_required
def block_user(user_id):
    me = g.user["id"]
    if user_id == me:
        abort(400)
    db = get_db()
    db.execute("INSERT OR IGNORE INTO blocks (blocker_id, blocked_id) VALUES (?, ?)", (me, user_id))
    db.execute(
        """DELETE FROM friendships
           WHERE (requester_id = ? AND addressee_id = ?)
              OR (requester_id = ? AND addressee_id = ?)""",
        (me, user_id, user_id, me))
    db.commit()
    flash("User blocked. You won't see each other anymore.", "ok")
    return redirect(url_for("discover"))


# --------------------------------------------------------------------------- #
# Chat
# --------------------------------------------------------------------------- #
def require_friend(user_id):
    other = get_user_or_404(user_id)
    if friend_status(g.user["id"], user_id) != "friends":
        abort(403)
    return other


def message_json(m):
    return {"id": m["id"], "mine": m["sender_id"] == g.user["id"],
            "body": m["body"], "time": m["created_at"][11:16],
            "read": bool(m["is_read"])}


@app.route("/chat/<int:user_id>")
@login_required
def chat(user_id):
    other = require_friend(user_id)
    return render_template("chat.html", other=other)


@app.route("/api/chat/<int:user_id>/messages", methods=["GET", "POST"])
@login_required
def chat_messages(user_id):
    require_friend(user_id)
    me = g.user["id"]
    db = get_db()

    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        body = str(data.get("body", "")).strip()
        if not body or len(body) > 1000:
            return jsonify(error="Message must be 1–1000 characters."), 400
        cur = db.execute(
            "INSERT INTO messages (sender_id, receiver_id, body, created_at) VALUES (?, ?, ?, ?)",
            (me, user_id, body, now()))
        db.commit()
        m = db.execute("SELECT * FROM messages WHERE id = ?", (cur.lastrowid,)).fetchone()
        return jsonify(message=message_json(m)), 201

    after = request.args.get("after", 0, type=int)
    rows = db.execute(
        """SELECT * FROM messages
           WHERE id > ? AND ((sender_id = ? AND receiver_id = ?)
                          OR (sender_id = ? AND receiver_id = ?))
           ORDER BY id LIMIT 200""",
        (after, me, user_id, user_id, me)).fetchall()
    db.execute("UPDATE messages SET is_read = 1 WHERE sender_id = ? AND receiver_id = ? AND is_read = 0",
               (user_id, me))
    db.commit()
    read_up_to = db.execute(
        "SELECT MAX(id) FROM messages WHERE sender_id = ? AND receiver_id = ? AND is_read = 1",
        (me, user_id)).fetchone()[0] or 0
    return jsonify(messages=[message_json(m) for m in rows], read_up_to=read_up_to)


# --------------------------------------------------------------------------- #
# Online presence + RandomCall (Socket.IO)
#
# ONLINE / SID_USER track which users have live sockets.  RC_QUEUE holds
# sockets waiting for a random call; RC_CALLS maps a socket to its active
# call; UID_BUSY counts how many of a user's sockets are mid-call so busy
# users are never matched again.  All shared state is guarded by STATE_LOCK
# (Socket.IO runs in threading mode).
# --------------------------------------------------------------------------- #
STATE_LOCK = threading.Lock()
ONLINE = {}      # user_id -> set of socket ids
SID_USER = {}    # socket id -> user_id
RC_QUEUE = []    # socket ids waiting for a match (FIFO)
RC_CALLS = {}    # socket id -> {"room": str, "peer": socket id, "kind": "rc"|"dc"}
UID_BUSY = {}    # user_id -> number of their sockets currently in a call
DC_INVITES = {}  # invite id -> {caller_sid, caller_uid, callee_uid, video} (direct calls, ringing)


def rc_peer_json(user):
    return {
        "id": user["id"],
        "name": user["display_name"],
        "age": user["age"],
        "city": user["city"] or "Somewhere",
        "photo": url_for("static", filename="uploads/" + user["photo"]) if user["photo"] else "",
        "hue": (user["id"] * 67) % 360,
        "initial": user["display_name"][:1].upper(),
    }


@app.route("/randomcall")
@login_required
def randomcall():
    return render_template("randomcall.html")


@socketio.on("connect")
def ws_connect():
    uid = session.get("user_id")
    if not uid:
        return False  # refuse anonymous sockets
    with STATE_LOCK:
        SID_USER[request.sid] = uid
        first_socket = uid not in ONLINE
        ONLINE.setdefault(uid, set()).add(request.sid)
    if first_socket:
        emit("presence", {"id": uid, "online": True}, broadcast=True)


@socketio.on("disconnect")
def ws_disconnect(reason=None):
    sid = request.sid
    _rc_teardown(sid)
    last_socket = False
    with STATE_LOCK:
        uid = SID_USER.pop(sid, None)
        if uid is not None and uid in ONLINE:
            ONLINE[uid].discard(sid)
            if not ONLINE[uid]:
                del ONLINE[uid]
                last_socket = True
    if last_socket:
        emit("presence", {"id": uid, "online": False}, broadcast=True)


@socketio.on("rc_find")
def rc_find():
    """Join the random-call queue; match instantly if someone suitable waits."""
    sid = request.sid
    uid = SID_USER.get(sid)
    if uid is None:
        return
    db = get_db()
    me = db.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    if me is None or not me["random_calls"]:
        emit("rc_error", {"message": "Random calls are turned off in your profile."})
        return
    # users this user has blocked / is blocked by (checked outside the lock)
    blocked = {r[0] for r in db.execute(
        """SELECT CASE WHEN blocker_id = ? THEN blocked_id ELSE blocker_id END
           FROM blocks WHERE blocker_id = ? OR blocked_id = ?""", (uid, uid, uid))}

    partner_sid = partner_uid = room = None
    with STATE_LOCK:
        if sid in RC_CALLS:
            return  # already in a call
        if sid in RC_QUEUE:
            RC_QUEUE.remove(sid)
        if not UID_BUSY.get(uid):
            for cand in RC_QUEUE:
                cand_uid = SID_USER.get(cand)
                if (cand_uid is None or cand_uid == uid or cand_uid in blocked
                        or UID_BUSY.get(cand_uid)):
                    continue
                partner_sid, partner_uid = cand, cand_uid
                break
        if partner_sid is None:
            RC_QUEUE.append(sid)
        else:
            RC_QUEUE.remove(partner_sid)
            room = "rc_" + secrets.token_hex(8)
            RC_CALLS[sid] = {"room": room, "peer": partner_sid, "kind": "rc"}
            RC_CALLS[partner_sid] = {"room": room, "peer": sid, "kind": "rc"}
            UID_BUSY[uid] = UID_BUSY.get(uid, 0) + 1
            UID_BUSY[partner_uid] = UID_BUSY.get(partner_uid, 0) + 1

    if partner_sid is None:
        emit("rc_waiting")
        return
    partner = db.execute("SELECT * FROM users WHERE id = ?", (partner_uid,)).fetchone()
    join_room(room, sid=sid)
    join_room(room, sid=partner_sid)
    # the user who just searched is the WebRTC initiator (creates the offer)
    emit("rc_match", {"room": room, "initiator": True, "peer": rc_peer_json(partner)}, to=sid)
    emit("rc_match", {"room": room, "initiator": False, "peer": rc_peer_json(me)}, to=partner_sid)


@socketio.on("rc_cancel")
def rc_cancel():
    with STATE_LOCK:
        if request.sid in RC_QUEUE:
            RC_QUEUE.remove(request.sid)


def _relay_signal(kind, data):
    """Relay WebRTC offers/answers/ICE candidates to the call partner."""
    call = RC_CALLS.get(request.sid)
    if not call or call["kind"] != kind or not isinstance(data, dict):
        return
    payload = {k: data[k] for k in ("sdp", "candidate") if k in data}
    if payload:
        emit(kind + "_signal", payload, to=call["peer"])


@socketio.on("rc_signal")
def rc_signal(data):
    _relay_signal("rc", data)


@socketio.on("dc_signal")
def dc_signal(data):
    _relay_signal("dc", data)


@socketio.on("rc_hangup")
def rc_hangup():
    _rc_teardown(request.sid)


@socketio.on("dc_hangup")
def dc_hangup():
    _rc_teardown(request.sid)


def _rc_teardown(sid):
    """End sid's call (if any), notify the peer, and leave queue/ringing state."""
    peer = kind = None
    with STATE_LOCK:
        if sid in RC_QUEUE:
            RC_QUEUE.remove(sid)
        call = RC_CALLS.pop(sid, None)
        if call:
            peer, kind = call["peer"], call["kind"]
            RC_CALLS.pop(peer, None)
            for s in (sid, peer):
                u = SID_USER.get(s)
                if u and UID_BUSY.get(u):
                    UID_BUSY[u] -= 1
                    if not UID_BUSY[u]:
                        del UID_BUSY[u]
        # drop direct-call invites this socket sent that are still ringing
        stale = [(iid, inv) for iid, inv in DC_INVITES.items() if inv["caller_sid"] == sid]
        for iid, _ in stale:
            del DC_INVITES[iid]
        ring_sids = [(iid, s) for iid, inv in stale for s in ONLINE.get(inv["callee_uid"], ())]
    if peer:
        socketio.emit("rc_peer_left" if kind == "rc" else "dc_ended", to=peer)
    for iid, s in ring_sids:
        socketio.emit("dc_dismiss", {"invite": iid}, to=s)


# ---- direct (friend-to-friend) voice & video calls ------------------------ #
@socketio.on("dc_invite")
def dc_invite(data):
    """Ring a friend for a voice (video=False) or video (video=True) call."""
    sid = request.sid
    uid = SID_USER.get(sid)
    if uid is None or not isinstance(data, dict):
        return
    try:
        target = int(data.get("to"))
    except (TypeError, ValueError):
        return
    video = bool(data.get("video"))
    other = get_db().execute("SELECT * FROM users WHERE id = ?", (target,)).fetchone()
    if other is None or friend_status(uid, target) != "friends" or is_blocked(uid, target):
        emit("dc_failed", {"message": "You can only call people on your friends list."})
        return
    with STATE_LOCK:
        if sid in RC_CALLS or UID_BUSY.get(uid):
            emit("dc_failed", {"message": "You're already in a call."})
            return
        if any(inv["caller_sid"] == sid for inv in DC_INVITES.values()):
            emit("dc_failed", {"message": "You're already calling someone."})
            return
        if target not in ONLINE:
            emit("dc_failed", {"message": f"{other['display_name']} is offline right now."})
            return
        if UID_BUSY.get(target):
            emit("dc_failed", {"message": f"{other['display_name']} is on another call."})
            return
        invite_id = secrets.token_hex(8)
        DC_INVITES[invite_id] = {"caller_sid": sid, "caller_uid": uid,
                                 "callee_uid": target, "video": video}
        callee_sids = list(ONLINE[target])
    me = get_db().execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    for s in callee_sids:  # ring every tab/device the friend has open
        emit("dc_incoming", {"invite": invite_id, "video": video,
                             "caller": rc_peer_json(me)}, to=s)
    emit("dc_ringing", {"invite": invite_id})


@socketio.on("dc_accept")
def dc_accept(data):
    sid = request.sid
    uid = SID_USER.get(sid)
    invite_id = (data or {}).get("invite")
    room = None
    with STATE_LOCK:
        inv = DC_INVITES.get(invite_id)
        if (not inv or inv["callee_uid"] != uid or sid in RC_CALLS
                or UID_BUSY.get(uid) or UID_BUSY.get(inv["caller_uid"])
                or inv["caller_sid"] not in SID_USER):
            emit("dc_failed", {"message": "That call is no longer available."})
            return
        del DC_INVITES[invite_id]
        caller_sid = inv["caller_sid"]
        room = "dc_" + secrets.token_hex(8)
        RC_CALLS[caller_sid] = {"room": room, "peer": sid, "kind": "dc"}
        RC_CALLS[sid] = {"room": room, "peer": caller_sid, "kind": "dc"}
        UID_BUSY[uid] = UID_BUSY.get(uid, 0) + 1
        UID_BUSY[inv["caller_uid"]] = UID_BUSY.get(inv["caller_uid"], 0) + 1
        other_tabs = [s for s in ONLINE.get(uid, ()) if s != sid]
    db = get_db()
    caller = db.execute("SELECT * FROM users WHERE id = ?", (inv["caller_uid"],)).fetchone()
    callee = db.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    join_room(room, sid=sid)
    join_room(room, sid=caller_sid)
    # the caller is the WebRTC initiator (creates the offer)
    emit("dc_accepted", {"video": inv["video"], "initiator": True,
                         "peer": rc_peer_json(callee)}, to=caller_sid)
    emit("dc_accepted", {"video": inv["video"], "initiator": False,
                         "peer": rc_peer_json(caller)}, to=sid)
    for s in other_tabs:  # stop ringing in the callee's other tabs
        emit("dc_dismiss", {"invite": invite_id}, to=s)


@socketio.on("dc_decline")
def dc_decline(data):
    uid = SID_USER.get(request.sid)
    invite_id = (data or {}).get("invite")
    with STATE_LOCK:
        inv = DC_INVITES.get(invite_id)
        if not inv or inv["callee_uid"] != uid:
            return
        del DC_INVITES[invite_id]
        other_tabs = [s for s in ONLINE.get(uid, ()) if s != request.sid]
    emit("dc_declined", to=inv["caller_sid"])
    for s in other_tabs:
        emit("dc_dismiss", {"invite": invite_id}, to=s)


@socketio.on("dc_cancel")
def dc_cancel():
    """Caller gives up while the call is still ringing."""
    sid = request.sid
    with STATE_LOCK:
        stale = [(iid, inv) for iid, inv in DC_INVITES.items() if inv["caller_sid"] == sid]
        for iid, _ in stale:
            del DC_INVITES[iid]
        ring_sids = [(iid, s) for iid, inv in stale for s in ONLINE.get(inv["callee_uid"], ())]
    for iid, s in ring_sids:
        emit("dc_dismiss", {"invite": iid}, to=s)


# --------------------------------------------------------------------------- #
# Demo data
# --------------------------------------------------------------------------- #
DEMO_USERS = [
    ("priya", "Priya", 24, "Bengaluru", "Bookworm and weekend trekker. Always up for chai and a long chat.",
     "reading, hiking, chai, photography"),
    ("arjun", "Arjun", 27, "Chennai", "Software dev who plays guitar badly but enthusiastically.",
     "coding, guitar, cricket, movies"),
    ("meera", "Meera", 22, "Bengaluru", "Art student. Let's visit galleries and street-food stalls!",
     "art, photography, food, travel"),
    ("rahul", "Rahul", 30, "Mumbai", "Runner, foodie and amateur stand-up comedian.",
     "running, food, comedy, cricket"),
    ("sara", "Sara", 26, "Hyderabad", "New in town, looking for friends to explore the city with.",
     "travel, movies, yoga, reading"),
    ("kiran", "Kiran", 29, "Chennai", "Board games, sci-fi and terrible puns.",
     "board games, coding, sci-fi, movies"),
    ("ananya", "Ananya", 25, "Pune", "Music lover — indie, carnatic, anything with a good beat.",
     "music, guitar, travel, yoga"),
    ("vikram", "Vikram", 33, "Bengaluru", "Cyclist and coffee snob. Show me your favourite café.",
     "cycling, coffee, hiking, photography"),
]


def seed():
    init_db()
    db = sqlite3.connect(DB_PATH)
    pw = generate_password_hash("password123")
    for username, name, age, city, bio, interests in DEMO_USERS:
        db.execute(
            """INSERT OR IGNORE INTO users (username, password_hash, display_name, age, city,
                                            bio, interests, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (username, pw, name, age, city, bio, interests, now()))
    ids = {r[0]: r[1] for r in db.execute("SELECT username, id FROM users")}
    for a, b, status in [("priya", "meera", "accepted"), ("arjun", "kiran", "accepted"),
                         ("sara", "priya", "pending"), ("vikram", "priya", "pending")]:
        db.execute(
            "INSERT OR IGNORE INTO friendships (requester_id, addressee_id, status, created_at) "
            "VALUES (?, ?, ?, ?)", (ids[a], ids[b], status, now()))
    db.execute(
        "INSERT INTO messages (sender_id, receiver_id, body, created_at) VALUES (?, ?, ?, ?)",
        (ids["meera"], ids["priya"], "Hey! Want to check out the photo walk this Sunday? 📷", now()))
    db.commit()
    db.close()
    print(f"Seeded {len(DEMO_USERS)} demo users. Log in as any of them "
          f"(e.g. 'priya') with password 'password123'.")


init_db()

# On fresh cloud deploys (CONNECT_SEED_DEMO=1) fill an empty database with demo users
if os.environ.get("CONNECT_SEED_DEMO") == "1":
    with sqlite3.connect(DB_PATH) as _db:
        if _db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
            seed()

if __name__ == "__main__":
    if "--seed" in sys.argv:
        seed()
    else:
        # FLASK_DEBUG=0 turns off debug mode (required when exposed to the internet)
        debug = os.environ.get("FLASK_DEBUG", "1") == "1"
        # cloud hosts (Render/Railway) set PORT and need 0.0.0.0; locally stay on 127.0.0.1
        port = int(os.environ.get("PORT", "5000"))
        host = "0.0.0.0" if "PORT" in os.environ else "127.0.0.1"
        socketio.run(app, host=host, port=port, debug=debug,
                     allow_unsafe_werkzeug=not debug)

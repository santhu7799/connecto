# Connect – online friendship app (Python + Flask)

Meet people who share your interests, send friend requests, chat — and jump into
random voice calls with people who are online right now.

## Features
- Sign up / log in (hashed passwords, 18+ only)
- Profile: photo, name, age, city, bio, interests, RandomCall availability toggle
- **Discover**: people ranked by shared interests (+ bonus for same city), with a % match, search by name/interest and city filter
- Friend requests: send, accept, decline, cancel, unfriend
- 1-to-1 chat between friends (auto-refreshes every 2 s), unread badges, ✓ / ✓✓ delivery status
- **Voice & video calls with friends**: 📞 and 🎥 buttons in the chat header ring the
  friend on every page/tab they have open — accept or decline, mute, toggle camera,
  end. Incoming-call popup appears wherever they are in the app, not just in chat.
- **Online status**: live green dot on avatars everywhere, powered by Socket.IO presence
- **RandomCall**: one click matches you with a random online user (no friendship needed)
  and starts a live WebRTC voice call — mute, end, or skip to the next person.
  Offline, busy, blocked and opted-out users are never matched.
- Block users (both sides disappear from each other — including RandomCall matching)
- CSRF protection on every form / API call

## Run it
```bash
cd connect_app
python -m venv venv
# Windows: venv\Scripts\activate    macOS/Linux: source venv/bin/activate
pip install -r requirements.txt
python app.py --seed     # optional: 8 demo users, password "password123"
python app.py            # open http://127.0.0.1:5000
```
Tip: log in as `priya` in one browser and `meera` in a private window to try
chatting — then open **RandomCall** in both windows to test a voice call with
yourself. The browser asks for microphone permission the first time.

> **Note on voice calls:** browsers only allow microphone access on `localhost`
> or HTTPS. On a LAN/in production, serve the app over HTTPS. Calls use a public
> STUN server (`stun.l.google.com:19302`); if callers are behind strict NATs you
> may also need a TURN server.

## How RandomCall works
1. Every logged-in page opens a Socket.IO connection → the server tracks who is online.
2. Clicking **Start a random call** asks for the mic, then emits `rc_find`.
3. The server keeps a queue of searching users and pairs the first eligible one
   (online, not you, not blocked either way, not already in a call, random calls enabled).
4. Both sides get `rc_match` with the partner's name, age, city and photo; the
   searcher creates a WebRTC offer and the server relays SDP/ICE via `rc_signal`.
5. Audio then flows peer-to-peer. Hanging up / disconnecting notifies the partner,
   who is automatically put back in the queue to find the next person.

## Project structure
```
connect_app/
├── app.py              # all routes, DB schema, matching logic, chat API,
│                       # Socket.IO presence + RandomCall matchmaking/signaling
├── connect.db          # SQLite database (created automatically)
├── requirements.txt
├── static/style.css
├── static/call.js      # friend voice/video calls (ring, accept/decline, WebRTC)
├── static/uploads/     # profile photos
└── templates/          # Jinja2 HTML pages
    ├── base.html  index.html  register.html  login.html
    ├── discover.html  user.html  profile.html
    ├── friends.html  chat.html  _friend_button.html
    └── randomcall.html # WebRTC voice call UI
```

## Database tables
| table | purpose |
|---|---|
| `users` | accounts and profiles (incl. photo + `random_calls` opt-in) |
| `friendships` | requester → addressee, status `pending` / `accepted` |
| `messages` | chat messages with read flag |
| `blocks` | who blocked whom |

Presence and call state (who's online, the RandomCall queue, active calls) live
in server memory — they reset on restart, which is exactly what you want.

## Ideas for next steps
1. **Group chats / interest rooms** – a `rooms` + `room_members` table.
3. **Report users + admin panel** for moderation (especially important with random calls).
4. **TURN server** (e.g. coturn) so calls connect across strict NATs.
5. **Deploy** – set a strong `SECRET_KEY` env var, run with `gunicorn -k gevent -w 1 app:app` (Socket.IO needs a compatible worker), and move to PostgreSQL.

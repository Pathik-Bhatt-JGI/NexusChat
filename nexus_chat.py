#!/usr/bin/env python3
"""
NEXUS CHAT v5.1 — Ultra-Secure Ephemeral Chat
Run: streamlit run nexus_chat.py
pip install streamlit streamlit-autorefresh
"""

import streamlit as st
import streamlit.components.v1 as components
import json, os, time, random, base64
from datetime import datetime

st.set_page_config(
    page_title="NEXUS CHAT",
    page_icon="⬡",
    layout="wide",
    initial_sidebar_state="collapsed",
)

try:
    from streamlit_autorefresh import st_autorefresh
    HAS_AR = True
except ImportError:
    HAS_AR = False

# ─────────────────────────────────────────────────────────────────────────────
#  PATHS
# ─────────────────────────────────────────────────────────────────────────────
DATA      = "chat_data"
ROOMS_F   = f"{DATA}/rooms.json"
MSGS_DIR  = f"{DATA}/messages"
MEDIA_DIR = f"{DATA}/media"
FRIENDS_F = f"{DATA}/friends.json"
INVITES_F = f"{DATA}/invites.json"

# Ensure ALL required directories exist at startup
for _d in [DATA, MSGS_DIR, MEDIA_DIR]:
    os.makedirs(_d, exist_ok=True)

# ─────────────────────────────────────────────────────────────────────────────
#  JSON HELPERS  (atomic writes — Windows-safe)
#  FIX #1: PermissionError on Windows when .tmp file is stale/locked.
#  Strategy: try atomic rename first; fall back to direct write on any
#  OS/permission error so the app never crashes on startup.
# ─────────────────────────────────────────────────────────────────────────────
def _jload(path, default):
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:
        return default

def _jsave(path, obj):
    # Ensure parent directory always exists
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    # Remove any leftover .tmp from a previous crashed run (Windows holds locks)
    try:
        if os.path.exists(tmp):
            os.remove(tmp)
    except OSError:
        pass
    try:
        # Preferred: atomic write via rename
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except (OSError, PermissionError):
        # Fallback: direct write (non-atomic but always works)
        try:
            os.remove(tmp)
        except OSError:
            pass
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, indent=2, ensure_ascii=False)

# ─────────────────────────────────────────────────────────────────────────────
#  ROOM HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def load_rooms():      return _jload(ROOMS_F, {})
def save_rooms(r):     _jsave(ROOMS_F, r)
def get_room(code):    return load_rooms().get(code)
def room_exists(code): return code in load_rooms()

def create_room(code, name, creator, auto_delete="never"):
    rooms = load_rooms()
    rooms[code] = {
        "name":        name,
        "creator":     creator,
        "created":     time.time(),
        "online":      {},
        "kicked":      [],
        "locked":      False,
        "auto_delete": auto_delete,
        "last_cleared": 0,
    }
    save_rooms(rooms)
    push_msg(code, "SYSTEM", f"Room '{name}' created by {creator}", "sys")

def delete_room(code):
    rooms = load_rooms()
    rooms.pop(code, None)
    save_rooms(rooms)
    try: os.remove(f"{MSGS_DIR}/{code}.json")
    except: pass

def lock_room(code, state):
    rooms = load_rooms()
    if code in rooms:
        rooms[code]["locked"] = state
        save_rooms(rooms)

def kick_user(code, user):
    rooms = load_rooms()
    if code not in rooms: return
    rooms[code].get("online", {}).pop(user, None)
    if user not in rooms[code].get("kicked", []):
        rooms[code].setdefault("kicked", []).append(user)
    save_rooms(rooms)
    push_msg(code, "SYSTEM", f"{user} was removed from the room", "sys")

def unkick_user(code, user):
    rooms = load_rooms()
    if code not in rooms: return
    kicked = rooms[code].get("kicked", [])
    if user in kicked:
        kicked.remove(user)
        rooms[code]["kicked"] = kicked
        save_rooms(rooms)

def is_kicked(code, user):
    return user in load_rooms().get(code, {}).get("kicked", [])

def is_creator(code, user):
    """Always re-derive admin status from persistent room data."""
    room = get_room(code)
    return room is not None and room.get("creator") == user

def heartbeat(code, user):
    try:
        rooms = load_rooms()
        if code not in rooms: return
        now = time.time()
        rooms[code].setdefault("online", {})[user] = now
        rooms[code]["online"] = {
            u: t for u, t in rooms[code]["online"].items() if now - t < 30
        }
        save_rooms(rooms)
    except Exception:
        pass

def leave_room(code, user):
    try:
        rooms = load_rooms()
        if code in rooms:
            rooms[code].get("online", {}).pop(user, None)
            save_rooms(rooms)
    except Exception:
        pass

def get_online(code):
    rooms = load_rooms()
    if code not in rooms: return []
    now = time.time()
    return sorted(u for u, t in rooms[code].get("online", {}).items() if now - t < 30)

# ─────────────────────────────────────────────────────────────────────────────
#  AUTO-DELETE
# ─────────────────────────────────────────────────────────────────────────────
_ADEL_INTERVALS = {"immediate": 300, "1h": 3600, "1d": 86400}
_ADEL_LABELS    = {
    "never":     "Never — keep until cleared",
    "immediate": "Every 5 Minutes (ephemeral)",
    "1h":        "After 1 Hour",
    "1d":        "After 1 Day",
}

def check_auto_delete(code):
    try:
        room = get_room(code)
        if not room: return
        policy = room.get("auto_delete", "never")
        if policy == "never": return
        interval = _ADEL_INTERVALS.get(policy, 0)
        if time.time() - room.get("last_cleared", 0) >= interval:
            save_msgs(code, [])
            rooms = load_rooms()
            if code in rooms:
                rooms[code]["last_cleared"] = time.time()
                save_rooms(rooms)
    except Exception:
        pass

# ─────────────────────────────────────────────────────────────────────────────
#  MESSAGE HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def load_msgs(code):    return _jload(f"{MSGS_DIR}/{code}.json", [])
def save_msgs(code, m): _jsave(f"{MSGS_DIR}/{code}.json", m)

def push_msg(code, user, text, mtype="msg"):
    msgs = load_msgs(code)
    msgs.append({
        "u": user, "t": text, "k": mtype,
        "h": datetime.now().strftime("%H:%M"), "ts": time.time(),
    })
    if len(msgs) > 500: msgs = msgs[-500:]
    save_msgs(code, msgs)

def push_media_msg(code, user, media_id):
    msgs = load_msgs(code)
    msgs.append({
        "u": user, "t": f"__MEDIA__{media_id}",
        "k": "media", "h": datetime.now().strftime("%H:%M"), "ts": time.time(),
    })
    if len(msgs) > 500: msgs = msgs[-500:]
    save_msgs(code, msgs)

def clear_msgs_admin(code):
    save_msgs(code, [])
    push_msg(code, "SYSTEM", "Chat history cleared by admin", "sys")

# ─────────────────────────────────────────────────────────────────────────────
#  MEDIA HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def save_media(media_id, data_b64, mime):
    _jsave(f"{MEDIA_DIR}/{media_id}.json", {"data": data_b64, "mime": mime})

def consume_media(media_id):
    path = f"{MEDIA_DIR}/{media_id}.json"
    obj  = _jload(path, None)
    if obj is None: return None
    try: os.remove(path)
    except: pass
    return {"data": obj["data"], "mime": obj["mime"]}

def media_exists(media_id):
    return os.path.exists(f"{MEDIA_DIR}/{media_id}.json")

# ─────────────────────────────────────────────────────────────────────────────
#  FRIENDS HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def _friends_repair():
    """Sanitise friends.json — tolerates missing file or corrupt data."""
    if not os.path.exists(FRIENDS_F):
        return  # Nothing to repair; file will be created on first write
    try:
        raw = _jload(FRIENDS_F, {})
        if not isinstance(raw, dict):
            _jsave(FRIENDS_F, {})
            return
        clean = {}
        for u, lst in raw.items():
            if isinstance(lst, list):
                clean[u] = [
                    e for e in lst
                    if isinstance(e, dict) and "name" in e and "code" in e
                ]
            else:
                clean[u] = []
        _jsave(FRIENDS_F, clean)
    except Exception:
        # Last-resort: overwrite with empty dict rather than crash
        try:
            _jsave(FRIENDS_F, {})
        except Exception:
            pass

_friends_repair()

def load_friends(username):
    if not username: return []
    raw = _jload(FRIENDS_F, {}).get(username, [])
    if not isinstance(raw, list): return []
    return [f for f in raw if isinstance(f, dict) and "name" in f and "code" in f]

def save_friend(username, friend_name, room_code):
    if not username: return False
    all_f   = _jload(FRIENDS_F, {})
    friends = all_f.get(username, [])
    if not isinstance(friends, list): friends = []
    if any(f.get("name") == friend_name for f in friends):
        return False
    friends.append({"name": str(friend_name), "code": str(room_code)})
    all_f[username] = friends
    _jsave(FRIENDS_F, all_f)
    return True

def delete_friend(username, friend_name):
    all_f   = _jload(FRIENDS_F, {})
    friends = all_f.get(username, [])
    if not isinstance(friends, list): friends = []
    all_f[username] = [f for f in friends if f.get("name") != friend_name]
    _jsave(FRIENDS_F, all_f)

# ─────────────────────────────────────────────────────────────────────────────
#  INVITE HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def load_invites(): return _jload(INVITES_F, {})

def send_invite(room_code, from_user, to_user, room_name):
    invites      = load_invites()
    key          = f"{room_code}:{to_user}"
    invites[key] = {
        "from": from_user, "to": to_user,
        "room": room_code, "room_name": room_name,
        "ts":   time.time(),
    }
    _jsave(INVITES_F, invites)

def get_pending_invites(username):
    now = time.time()
    return [
        inv for inv in load_invites().values()
        if inv.get("to") == username and now - inv.get("ts", 0) < 86400
    ]

def dismiss_invite(room_code, username):
    invites = load_invites()
    invites.pop(f"{room_code}:{username}", None)
    _jsave(INVITES_F, invites)

# ─────────────────────────────────────────────────────────────────────────────
#  UTILITIES
# ─────────────────────────────────────────────────────────────────────────────
def gen_code():
    return "".join(random.choices("ABCDEFGHJKLMNPQRSTUVWXYZ23456789", k=6))

def gen_media_id():
    return "".join(random.choices("abcdefghijklmnopqrstuvwxyz0123456789", k=20))

def h(s):
    """HTML-escape a value for safe injection into markup."""
    return (str(s)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;"))

# ─────────────────────────────────────────────────────────────────────────────
#  SESSION STATE
# ─────────────────────────────────────────────────────────────────────────────
_DEFAULTS = dict(
    page="home", room_code="", username="", is_creator=False,
    show_code=False, error="", success="",
    confirm_del=False, confirm_clr=False,
    home_username="", media_cache={},
    last_heartbeat=0,
    sent_media=set(),
)
for _k, _v in _DEFAULTS.items():
    if _k not in st.session_state:
        st.session_state[_k] = _v
S = st.session_state

# ─────────────────────────────────────────────────────────────────────────────
#  STYLES
# ─────────────────────────────────────────────────────────────────────────────
_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Exo+2:wght@400;700;800&family=Rajdhani:wght@400;600&display=swap');

:root {
  --bg:#030508; --bg2:#070E17; --bg3:#0C1826;
  --green:#00FFB3; --green2:#00CC8F; --cyan:#00D4FF;
  --violet:#8B6FFF; --amber:#FFD700; --red:#FF3366;
  --t1:#EFF6FF; --t2:#8EB0CC; --t3:#3A5570; --t4:#1A2F45;
  --b1:rgba(0,255,179,.08); --b2:rgba(0,255,179,.16); --b3:rgba(0,255,179,.3);
  --mono:'Share Tech Mono',monospace;
  --dis:'Exo 2',sans-serif;
  --bod:'Rajdhani',sans-serif;
  --r1:4px; --r2:8px; --r3:14px;
}

* { box-sizing: border-box; }
.stApp { background: var(--bg) !important; color: var(--t1); font-family: var(--bod); }
#MainMenu, footer, header, .stDeployButton { display: none !important; }
.main .block-container { padding: 1.2rem 2rem 5rem; max-width: 1440px; }
::-webkit-scrollbar { width: 3px; height: 3px; }
::-webkit-scrollbar-track { background: var(--bg); }
::-webkit-scrollbar-thumb { background: var(--green2); border-radius: 2px; }
[data-testid="stSidebar"], [data-testid="collapsedControl"] { display: none !important; }
@media print { body * { visibility: hidden !important; } }

/* ── Buttons ── */
.stButton > button {
  background: transparent !important; color: var(--green) !important;
  border: 1px solid var(--b2) !important;
  font-family: var(--mono) !important; font-size: .7rem !important;
  letter-spacing: .1em !important; text-transform: uppercase !important;
  border-radius: var(--r1) !important; padding: .45rem 1rem !important;
  transition: all .2s !important;
}
.stButton > button:hover { background: rgba(0,255,179,.07) !important; border-color: var(--green) !important; }
.stButton > button[kind="primary"] { background: rgba(0,255,179,.09) !important; border-color: rgba(0,255,179,.5) !important; }

/* ── Inputs ── */
.stTextInput > div > div > input,
.stTextArea  > div > div > textarea {
  background: var(--bg3) !important; border: 1px solid var(--b1) !important;
  border-radius: var(--r1) !important; color: var(--t1) !important;
  font-family: var(--mono) !important; font-size: .82rem !important;
}
.stTextInput > div > div > input:focus {
  border-color: var(--b3) !important; box-shadow: 0 0 14px rgba(0,255,179,.08) !important;
}
.stSelectbox > div > div {
  background: var(--bg3) !important; border: 1px solid var(--b1) !important;
  border-radius: var(--r1) !important; color: var(--t1) !important;
  font-family: var(--mono) !important; font-size: .82rem !important;
}
.stTextInput label, .stSelectbox label, .stTextArea label,
[data-testid="stWidgetLabel"] {
  font-family: var(--mono) !important; font-size: .6rem !important;
  letter-spacing: .12em !important; text-transform: uppercase !important;
  color: var(--t3) !important;
}

/* ── File uploader ── */
[data-testid="stFileUploader"] {
  background: var(--bg3) !important;
  border: 1px dashed var(--b2) !important;
  border-radius: var(--r2) !important;
}
[data-testid="stFileUploaderDropzoneInstructions"] span,
[data-testid="stFileUploaderDropzoneInstructions"] small { display: none !important; }
[data-testid="stFileUploaderDropzoneInstructions"] > div::after {
  content: 'Drop file or click to browse';
  font-family: var(--mono); font-size: .62rem; color: var(--t3);
}

/* ── Expander ── */
[data-testid="stExpander"] {
  background: var(--bg2) !important; border: 1px solid var(--b1) !important;
  border-radius: var(--r2) !important;
}
[data-testid="stExpander"] summary { padding: 8px 14px !important; }
[data-testid="stExpander"] summary p {
  font-family: var(--mono) !important; font-size: .68rem !important;
  color: var(--green) !important; letter-spacing: .1em !important;
}
[data-testid="stExpander"] .streamlit-expanderContent { padding: 8px 14px 12px !important; }

hr { border: none !important; border-top: 1px solid var(--b1) !important; margin: 14px 0 !important; }

/* ── Layout helpers ── */
.nx-hero {
  background: linear-gradient(135deg,var(--bg2),rgba(0,255,179,.04) 50%,var(--bg2));
  border: 1px solid var(--b2); border-radius: var(--r3);
  padding: 26px 30px; margin-bottom: 20px; position: relative; overflow: hidden;
}
.nx-hero::before {
  content:''; position:absolute; top:0; left:0; right:0; height:1px;
  background: linear-gradient(90deg,transparent,var(--green),var(--cyan),transparent);
}
.nx-eyebrow { font-family:var(--mono); font-size:.56rem; letter-spacing:.28em; text-transform:uppercase; color:var(--green); margin-bottom:8px; }
.nx-title   { font-family:var(--dis);  font-size:2.2rem; font-weight:800; letter-spacing:-.02em; color:var(--t1); line-height:1; margin-bottom:6px; }
.nx-title span { color: var(--green); }
.nx-sub     { font-family:var(--bod);  color:var(--t2);  font-size:.88rem; line-height:1.6; }

.nx-panel {
  background: var(--bg2); border: 1px solid var(--b1); border-radius: var(--r2);
  padding: 18px 20px; margin-bottom: 12px; position: relative; overflow: hidden;
}
.nx-panel::before {
  content:''; position:absolute; top:0; left:0; right:0; height:1px;
  background: linear-gradient(90deg,transparent,var(--green),transparent); opacity:.18;
}
.nx-ptitle {
  font-family:var(--mono); font-size:.56rem; letter-spacing:.2em; text-transform:uppercase;
  color:var(--green); margin-bottom:12px; padding-bottom:7px; border-bottom:1px solid var(--b1);
}

.nx-kpi       { background:var(--bg2); border:1px solid var(--b1); border-radius:var(--r2); padding:14px 16px; }
.nx-kpi-lbl   { font-family:var(--mono); font-size:.56rem; letter-spacing:.15em; text-transform:uppercase; color:var(--t3); margin-bottom:7px; }
.nx-kpi-val   { font-family:var(--dis);  font-size:1.5rem; font-weight:700; color:var(--green); }
.nx-kpi-sub   { font-family:var(--mono); font-size:.64rem; color:var(--t3); margin-top:3px; }

/* Badges */
.bd { display:inline-flex; align-items:center; padding:2px 8px; border-radius:3px; font-family:var(--mono); font-size:.58rem; letter-spacing:.06em; white-space:nowrap; }
.bd-g { color:#00FFB3; border:1px solid rgba(0,255,179,.25); background:rgba(0,255,179,.07); }
.bd-c { color:#00D4FF; border:1px solid rgba(0,212,255,.25); background:rgba(0,212,255,.07); }
.bd-a { color:#FFD700; border:1px solid rgba(255,215,0,.25);  background:rgba(255,215,0,.07); }
.bd-r { color:#FF3366; border:1px solid rgba(255,51,102,.25); background:rgba(255,51,102,.07); }
.bd-v { color:#8B6FFF; border:1px solid rgba(139,111,255,.25);background:rgba(139,111,255,.07); }

/* Alert boxes */
.nx-box {
  border:1px solid; border-left:3px solid; border-radius:0 var(--r2) var(--r2) 0;
  padding:10px 14px; margin:8px 0; font-family:var(--mono); font-size:.7rem; line-height:1.6;
}
.nx-box-g { color:#00FFB3; border-color:rgba(0,255,179,.2); border-left-color:#00FFB3; background:rgba(0,255,179,.04); }
.nx-box-c { color:#00D4FF; border-color:rgba(0,212,255,.2); border-left-color:#00D4FF; background:rgba(0,212,255,.04); }
.nx-box-a { color:#FFD700; border-color:rgba(255,215,0,.2);  border-left-color:#FFD700; background:rgba(255,215,0,.04); }
.nx-box-r { color:#FF3366; border-color:rgba(255,51,102,.2); border-left-color:#FF3366; background:rgba(255,51,102,.04); }

.nx-success { background:rgba(0,255,179,.05); border:1px solid rgba(0,255,179,.22); border-radius:var(--r2); padding:16px 18px; margin:10px 0; }
.nx-s-title { font-family:var(--mono); font-size:.62rem; letter-spacing:.14em; text-transform:uppercase; color:#00FFB3; margin-bottom:6px; }
.nx-warn    { background:rgba(255,215,0,.04); border:1px solid rgba(255,215,0,.15); border-left:3px solid #FFD700; border-radius:0 var(--r2) var(--r2) 0; padding:8px 14px; margin:8px 0; font-family:var(--mono); font-size:.68rem; color:#FFD700; }

/* Room header */
.nx-rh     { display:flex; align-items:flex-start; justify-content:space-between; padding:10px 0; border-bottom:1px solid rgba(0,255,179,.08); margin-bottom:14px; }
.nx-rname  { font-family:'Exo 2',sans-serif; font-size:1.35rem; font-weight:700; color:#EFF6FF; }
.nx-rmeta  { font-family:'Share Tech Mono',monospace; font-size:.6rem; color:#3A5570; margin-top:5px; display:flex; flex-wrap:wrap; gap:6px; align-items:center; }
.nx-rstat  { text-align:right; font-family:'Share Tech Mono',monospace; font-size:.62rem; color:#3A5570; }

/* Session card */
.nx-sess       { background:var(--bg2); border:1px solid var(--b1); border-radius:var(--r2); padding:14px 16px; margin-top:8px; }
.nx-sess-title { font-family:var(--mono); font-size:.56rem; letter-spacing:.2em; text-transform:uppercase; color:#00FFB3; margin-bottom:10px; padding-bottom:6px; border-bottom:1px solid rgba(0,255,179,.08); }
.nx-sess-row   { display:flex; justify-content:space-between; font-family:var(--mono); font-size:.63rem; color:#3A5570; padding:3px 0; }
.nx-vg { color:#00FFB3; }
.nx-vc { color:#00D4FF; }
.nx-vw { color:#8EB0CC; }

/* Online row */
.nx-orow { font-family:'Share Tech Mono',monospace; font-size:.68rem; padding:5px 0; border-bottom:1px solid rgba(0,255,179,.08); display:flex; align-items:center; gap:5px; }

/* Invite card */
.nx-inv { background:rgba(139,111,255,.06); border:1px solid rgba(139,111,255,.2); border-radius:var(--r2); padding:10px 14px; margin:4px 0; font-family:'Share Tech Mono',monospace; font-size:.65rem; color:#8B6FFF; }

@media(max-width:768px) { .main .block-container { padding:.6rem .6rem 5rem; } .nx-title { font-size:1.5rem; } }
</style>
"""

def _style():
    st.markdown(_CSS, unsafe_allow_html=True)

def _guard():
    components.html("""
<div id="__g" style="display:none;position:fixed;inset:0;background:#000;z-index:999999;pointer-events:none;"></div>
<script>
(function(){
  var g=document.getElementById('__g');
  var show=function(){g&&(g.style.display='block');};
  var hide=function(){g&&(g.style.display='none');};
  window.addEventListener('beforeprint',show);
  window.addEventListener('afterprint',hide);
  document.addEventListener('keyup',function(e){
    if(e.key==='PrintScreen'){try{navigator.clipboard.writeText('');}catch(_){}show();setTimeout(hide,500);}
  },true);
  document.addEventListener('keydown',function(e){
    var k=e.key.toLowerCase(),c=e.ctrlKey||e.metaKey;
    if(k==='f12')e.preventDefault();
    if(c&&e.shiftKey&&['i','j','c'].includes(k))e.preventDefault();
  },true);
})();
</script>""", height=0, scrolling=False)

def _flash():
    if S.error:
        st.markdown(f'<div class="nx-box nx-box-r">{h(S.error)}</div>', unsafe_allow_html=True)
        S.error = ""
    if S.success:
        st.markdown(f'<div class="nx-box nx-box-g">{h(S.success)}</div>', unsafe_allow_html=True)
        S.success = ""

# ─────────────────────────────────────────────────────────────────────────────
#  HELPER: enter a room (used by join, friend-join, invite-accept)
#  FIX #3 + #4: admin status is always derived from room data, never hard-coded.
#  FIX #4: creator can always enter their own room even when locked.
# ─────────────────────────────────────────────────────────────────────────────
def _enter_room(code, username, announce=True):
    """
    Validate and enter a room. Returns an error string on failure, or None on success.
    On success the session state is updated and the caller should st.rerun().
    """
    code = code.strip().upper()
    username = username.strip()

    if not code:
        return "Access code required."
    if len(username) < 2:
        return "Username must be at least 2 characters."
    if not room_exists(code):
        return f"Room '{code}' not found."

    # Kicked check (admin is never locked out of their own room)
    if is_kicked(code, username) and not is_creator(code, username):
        return "You were removed from this room. Ask the host to invite you back."

    # Locked check — creator can ALWAYS enter their own room even when locked
    room = get_room(code)
    if room.get("locked") and not is_creator(code, username):
        return "Room is currently locked."

    # If creator was previously kicked (edge case), clear that
    if is_kicked(code, username) and is_creator(code, username):
        unkick_user(code, username)

    if announce:
        push_msg(code, "SYSTEM", f"{username} joined the room", "sys")

    # FIX #3: derive is_creator from persistent room data, not hard-coded False
    S.room_code   = code
    S.username    = username
    S.home_username = username
    S.is_creator  = is_creator(code, username)
    S.page        = "chat"
    S.sent_media  = set()
    S.error       = ""
    return None  # success

# ─────────────────────────────────────────────────────────────────────────────
#  CHAT IFRAME RENDERER
# ─────────────────────────────────────────────────────────────────────────────
def render_messages(msgs, username, media_cache, sent_media_ids, height=480):
    payload = [
        {"u": m.get("u",""), "t": m.get("t",""), "k": m.get("k","msg"), "h": m.get("h","")}
        for m in msgs[-200:]
    ]
    media_map = {
        mid: media_cache[mid]
        for m in payload
        if m["k"] == "media"
        for mid in [m["t"].replace("__MEDIA__","").strip()]
        if mid in media_cache and mid not in sent_media_ids
    }
    p_js  = json.dumps(payload)
    u_js  = json.dumps(username)
    m_js  = json.dumps(media_map)
    sm_js = json.dumps(list(sent_media_ids))

    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<link href="https://fonts.googleapis.com/css2?family=Share+Tech+Mono&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{{box-sizing:border-box;-webkit-user-select:none!important;user-select:none!important;}}
html,body{{margin:0;padding:0;background:#0A1520;overflow:hidden;height:{height}px;}}
#sc{{height:{height}px;overflow-y:auto;padding:12px 14px;scrollbar-width:thin;scrollbar-color:#00CC8F #060C12;}}
#sc::-webkit-scrollbar{{width:3px;}}#sc::-webkit-scrollbar-thumb{{background:#00CC8F;border-radius:2px;}}
.msg{{margin:6px 0;padding:9px 13px;border-radius:6px;max-width:82%;animation:pop .18s ease;}}
@keyframes pop{{from{{opacity:0;transform:translateY(5px)}}to{{opacity:1;transform:none}}}}
.own  {{margin-left:auto;background:#0D2218;border:1px solid rgba(0,255,179,.22);border-right:3px solid #00FFB3;}}
.other{{background:#0A1828;border:1px solid rgba(0,212,255,.18);border-left:3px solid #00D4FF;}}
.sys  {{max-width:100%;text-align:center;background:rgba(139,111,255,.07);border:1px solid rgba(139,111,255,.14);padding:5px 12px;}}
.mu{{font-family:'Share Tech Mono',monospace;font-size:10px;letter-spacing:.1em;margin-bottom:4px;}}
.own .mu{{color:#00CC8F;text-align:right;}}.other .mu{{color:#00D4FF;}}
.mt{{font-family:'Share Tech Mono',monospace;font-size:13px;color:#EFF6FF;line-height:1.55;word-break:break-word;}}
.sys .mt{{color:#9B8FFF;font-size:10px;}}
.mh{{font-family:'Share Tech Mono',monospace;font-size:9px;color:#2A4560;margin-top:4px;}}
.own .mh{{text-align:right;}}.other .mh{{text-align:left;}}
.mwrap{{max-width:240px;border:1px solid rgba(0,255,179,.15);border-radius:6px;overflow:hidden;}}
.mwrap img{{width:100%;display:block;pointer-events:none;-webkit-user-drag:none;}}
.mwrap video{{width:100%;display:block;}}
.mbadge{{font-family:'Share Tech Mono',monospace;font-size:9px;color:rgba(0,255,179,.8);padding:3px 8px;background:rgba(0,0,0,.6);letter-spacing:.1em;text-align:center;}}
.msent{{padding:10px;font-family:'Share Tech Mono',monospace;font-size:10px;color:rgba(0,255,179,.5);border:1px solid rgba(0,255,179,.1);border-radius:6px;text-align:center;background:rgba(0,255,179,.02);letter-spacing:.08em;}}
.mexp {{padding:10px;font-family:'Share Tech Mono',monospace;font-size:10px;color:rgba(255,51,102,.5);border:1px solid rgba(255,51,102,.12);border-radius:6px;text-align:center;background:rgba(255,51,102,.02);}}
#empty{{height:{height}px;display:flex;align-items:center;justify-content:center;font-family:'Share Tech Mono',monospace;font-size:10px;letter-spacing:.25em;color:#1A2F45;}}
#wm{{position:fixed;inset:0;pointer-events:none;z-index:9999;overflow:hidden;}}
.wl{{position:absolute;white-space:nowrap;font-family:'Share Tech Mono',monospace;font-size:9px;color:rgba(0,255,179,.04);transform:rotate(-32deg);letter-spacing:.06em;transform-origin:left center;}}
body.blurred #sc{{filter:blur(14px);transition:filter .25s;}}
#ss{{display:none;position:fixed;inset:0;background:#000;z-index:99999;pointer-events:none;}}
@media print{{#ss{{display:block!important;}}#sc,#wm{{display:none!important;}}}}
</style></head><body>
<div id="sc"><div id="empty">· NO MESSAGES YET ·</div></div>
<div id="wm"></div><div id="ss"></div>
<script>
(function(){{
  var MSGS={p_js}, ME={u_js}, MEDIA={m_js}, SENT=new Set({sm_js});
  var sc=document.getElementById('sc');
  function esc(s){{return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}}
  if(MSGS.length){{
    var emp=document.getElementById('empty'); if(emp) emp.remove();
    MSGS.forEach(function(m){{
      var d=document.createElement('div');
      var own=(m.u===ME), sys=(m.k==='sys'), med=(m.k==='media');
      if(sys){{
        d.className='msg sys';
        d.innerHTML='<div class="mt">— '+esc(m.t)+' —</div>';
      }} else if(med){{
        var mid=m.t.replace('__MEDIA__','').trim();
        d.className='msg '+(own?'own':'other');
        var inner;
        if(SENT.has(mid)){{
          inner='<div class="msent">⊕ FILE SENT<br><span style="font-size:8px;opacity:.5;">Recipient sees it once</span></div>';
        }} else {{
          var obj=MEDIA[mid];
          if(obj){{
            inner=obj.mime.startsWith('image/')
              ?'<div class="mwrap"><img src="data:'+obj.mime+';base64,'+obj.data+'" draggable="false" oncontextmenu="return false"><div class="mbadge">⊘ ONE-TIME VIEW</div></div>'
              :'<div class="mwrap"><video src="data:'+obj.mime+';base64,'+obj.data+'" autoplay muted playsinline controls></video><div class="mbadge">⊘ ONE-TIME VIEW</div></div>';
          }} else {{
            inner='<div class="mexp">⊘ MEDIA EXPIRED<br><span style="font-size:8px;opacity:.5;">Already viewed</span></div>';
          }}
        }}
        d.innerHTML='<div class="mu">'+esc(m.u)+'</div>'+inner+'<div class="mh">'+esc(m.h)+'</div>';
      }} else {{
        d.className='msg '+(own?'own':'other');
        d.innerHTML='<div class="mu">'+esc(m.u)+'</div><div class="mt">'+esc(m.t)+'</div><div class="mh">'+esc(m.h)+'</div>';
      }}
      sc.appendChild(d);
    }});
    sc.scrollTop=sc.scrollHeight;
  }}
  /* Watermark */
  var wm=document.getElementById('wm'), wt=ME+' · NEXUS CHAT · CONFIDENTIAL · ';
  for(var r=0;r<20;r++) for(var c=0;c<3;c++){{
    var s=document.createElement('div'); s.className='wl'; s.textContent=wt;
    s.style.left=(c*38-5)+'%'; s.style.top=(r*6-1)+'%'; wm.appendChild(s);
  }}
  var blk=function(e){{e.preventDefault();e.stopPropagation();}};
  ['copy','cut','contextmenu','selectstart','dragstart'].forEach(function(ev){{document.addEventListener(ev,blk,true);}});
  document.addEventListener('keydown',function(e){{
    var k=e.key.toLowerCase(),c=e.ctrlKey||e.metaKey;
    if(c&&['c','a','x','s','u','p'].includes(k)){{blk(e);return;}}
    if(k==='f12'){{blk(e);return;}}
    if(k==='printscreen'){{
      e.preventDefault(); try{{navigator.clipboard.writeText('');}}catch(_){{}}
      var ss=document.getElementById('ss'); ss.style.display='block';
      setTimeout(function(){{ss.style.display='none';}},500);
    }}
  }},true);
  document.addEventListener('visibilitychange',function(){{document.body.classList.toggle('blurred',document.hidden);}});
  window.addEventListener('beforeprint',function(){{document.getElementById('ss').style.display='block';}});
  window.addEventListener('afterprint', function(){{document.getElementById('ss').style.display='none';}});
  document.addEventListener('touchstart',function(e){{if(e.touches.length>1)e.preventDefault();}},{{passive:false}});
}})();
</script></body></html>"""


# ─────────────────────────────────────────────────────────────────────────────
#  PAGE: HOME
# ─────────────────────────────────────────────────────────────────────────────
def page_home():
    _style(); _guard()

    st.markdown("""
<div class="nx-hero">
  <div class="nx-eyebrow">&#x2B21; ULTRA-SECURE EPHEMERAL MESSAGING</div>
  <div class="nx-title">NEXUS <span>CHAT</span></div>
  <div class="nx-sub">Private rooms &nbsp;&middot;&nbsp; Friends &amp; Invites &nbsp;&middot;&nbsp; One-time media &nbsp;&middot;&nbsp; Auto-delete &nbsp;&middot;&nbsp; Screenshot protection</div>
</div>""", unsafe_allow_html=True)

    S.home_username = st.text_input(
        "Your Username",
        value=S.home_username, placeholder="e.g. Ghost_X",
        max_chars=20, key="home_uname")

    c1, c2, c3 = st.columns(3)
    with c1:
        st.markdown("""<div class="nx-panel">
  <div class="nx-ptitle">&#x2295; Create Room</div>
  <div style="font-family:'Share Tech Mono',monospace;font-size:.7rem;color:#8EB0CC;line-height:2;">
    Start a new secure room.<br>6-char access code.<br>Full admin controls.
  </div>
</div>""", unsafe_allow_html=True)
        if st.button("CREATE ROOM", key="go_create", type="primary", use_container_width=True):
            S.page = "create"; st.rerun()

    with c2:
        st.markdown("""<div class="nx-panel">
  <div class="nx-ptitle">&#x229E; Join Room</div>
  <div style="font-family:'Share Tech Mono',monospace;font-size:.7rem;color:#8EB0CC;line-height:2;">
    Enter a room code from<br>the creator, or pick a<br>friend from the list below.
  </div>
</div>""", unsafe_allow_html=True)
        if st.button("JOIN ROOM", key="go_join", use_container_width=True):
            S.page = "join"; st.rerun()

    with c3:
        st.markdown("""<div class="nx-panel">
  <div class="nx-ptitle">&#x2298; Security</div>
  <div style="font-family:'Share Tech Mono',monospace;font-size:.66rem;color:#8EB0CC;line-height:2.8;">
    <span class="bd bd-g">TEXT COPY BLOCKED</span><br>
    <span class="bd bd-c">USERNAME WATERMARK</span><br>
    <span class="bd bd-r">AUTO BLUR ON TAB SWITCH</span><br>
    <span class="bd bd-a">SCREENSHOT BLOCKED</span><br>
    <span class="bd bd-v">ONE-TIME MEDIA</span>
  </div>
</div>""", unsafe_allow_html=True)

    _flash()

    # ── Pending invites ────────────────────────────────────────────────────
    uname = S.home_username.strip()
    if uname:
        pending = get_pending_invites(uname)
        if pending:
            st.markdown("""<div class="nx-panel" style="border-color:rgba(139,111,255,.3);">
  <div class="nx-ptitle" style="color:#8B6FFF;">&#x2295; PENDING INVITES</div>""",
                unsafe_allow_html=True)
            for inv in pending:
                ic1, ic2, ic3 = st.columns([4, 1, 1])
                with ic1:
                    st.markdown(
                        f'<div class="nx-inv">'
                        f'From <b style="color:#EFF6FF;">{h(inv["from"])}</b>'
                        f' &rarr; join <b style="color:#00FFB3;">{h(inv["room_name"])}</b>'
                        f'&nbsp;<span class="bd bd-v">CODE: {h(inv["room"])}</span>'
                        f'</div>',
                        unsafe_allow_html=True)
                with ic2:
                    if st.button("ACCEPT", key=f"ia_{inv['room']}", use_container_width=True, type="primary"):
                        rc = inv["room"]
                        if not room_exists(rc):
                            S.error = f"Room {rc} no longer exists."
                        else:
                            unkick_user(rc, uname)
                            dismiss_invite(rc, uname)
                            push_msg(rc, "SYSTEM", f"{uname} rejoined via invite", "sys")
                            # FIX #3: derive admin status correctly
                            S.room_code  = rc
                            S.username   = uname
                            S.is_creator = is_creator(rc, uname)
                            S.sent_media = set()
                            S.page       = "chat"
                        st.rerun()
                with ic3:
                    if st.button("DECLINE", key=f"id_{inv['room']}", use_container_width=True):
                        dismiss_invite(inv["room"], uname); st.rerun()
            st.markdown("</div>", unsafe_allow_html=True)

    # ── Friends panel ──────────────────────────────────────────────────────
    st.markdown('<div class="nx-panel"><div class="nx-ptitle">&#x25C8; Friends &mdash; Quick Join</div>', unsafe_allow_html=True)

    if not uname:
        st.markdown('<div style="font-family:\'Share Tech Mono\',monospace;font-size:.68rem;color:#1A2F45;padding:4px 0 8px;">Enter your username above to see your friends list.</div>', unsafe_allow_html=True)
    else:
        friends = load_friends(uname)
        if not friends:
            st.markdown('<div style="font-family:\'Share Tech Mono\',monospace;font-size:.68rem;color:#1A2F45;padding:4px 0 8px;">No friends yet &mdash; add them from inside a chat room.</div>', unsafe_allow_html=True)
        else:
            for fr in friends:
                cn, cc, cj, cd = st.columns([3, 2, 1, 1])
                with cn:
                    st.markdown(f'<div style="font-family:\'Share Tech Mono\',monospace;font-size:.75rem;color:#00D4FF;padding:8px 2px;">{h(fr["name"])}</div>', unsafe_allow_html=True)
                with cc:
                    st.markdown(f'<div style="font-family:\'Share Tech Mono\',monospace;font-size:.63rem;color:#3A5570;padding:8px 2px;">CODE: <b style="color:#00FFB3;letter-spacing:.14em;">{h(fr["code"])}</b></div>', unsafe_allow_html=True)
                with cj:
                    if st.button("JOIN", key=f"fj_{fr['name']}", use_container_width=True):
                        # FIX #3 + #4: use _enter_room for consistent logic
                        err = _enter_room(fr["code"], uname)
                        if err:
                            S.error = err
                        st.rerun()
                with cd:
                    if st.button("DEL", key=f"fd_{fr['name']}", use_container_width=True):
                        delete_friend(uname, fr["name"]); st.rerun()

    st.markdown("</div>", unsafe_allow_html=True)

    # ── Stats ──────────────────────────────────────────────────────────────
    rooms   = load_rooms()
    now     = time.time()
    o_total = sum(len([u for u,t in r.get("online",{}).items() if now-t<30]) for r in rooms.values())
    m_total = sum(len(load_msgs(c)) for c in rooms)
    st.markdown("<br>", unsafe_allow_html=True)
    k1, k2, k3 = st.columns(3)
    with k1:
        st.markdown(f'<div class="nx-kpi"><div class="nx-kpi-lbl">Active Rooms</div><div class="nx-kpi-val">{len(rooms)}</div><div class="nx-kpi-sub">Currently running</div></div>', unsafe_allow_html=True)
    with k2:
        st.markdown(f'<div class="nx-kpi"><div class="nx-kpi-lbl">Online Users</div><div class="nx-kpi-val">{o_total}</div><div class="nx-kpi-sub">Active (30s window)</div></div>', unsafe_allow_html=True)
    with k3:
        st.markdown(f'<div class="nx-kpi"><div class="nx-kpi-lbl">Messages</div><div class="nx-kpi-val" style="color:#00D4FF;">{m_total}</div><div class="nx-kpi-sub">Across all rooms</div></div>', unsafe_allow_html=True)

    if not HAS_AR:
        st.markdown('<div class="nx-warn" style="margin-top:14px;">&#9888; pip install streamlit-autorefresh for live updates.</div>', unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────────────────────
#  PAGE: CREATE ROOM
# ─────────────────────────────────────────────────────────────────────────────
def page_create():
    _style(); _guard()
    st.markdown('<div class="nx-panel"><div class="nx-ptitle">&#x2295; Create New Room</div></div>', unsafe_allow_html=True)
    col1, col2 = st.columns([2, 1])
    with col1:
        room_name   = st.text_input("Room Name", placeholder="e.g. Project Alpha", max_chars=40, key="cr_name")
        username    = st.text_input("Your Username", placeholder="e.g. Ghost_X", max_chars=20, key="cr_user", value=S.home_username)
        auto_delete = st.selectbox("Auto-Delete Messages", options=list(_ADEL_LABELS.keys()), format_func=lambda x: _ADEL_LABELS[x], key="cr_autodel")
        _flash()
        b1, b2 = st.columns(2)
        with b1:
            if st.button("CREATE ROOM", type="primary", use_container_width=True, key="do_create"):
                rn = room_name.strip(); un = username.strip()
                if not rn: S.error = "Room name required."; st.rerun()
                elif len(un) < 2: S.error = "Username must be at least 2 characters."; st.rerun()
                else:
                    code = gen_code()
                    while room_exists(code): code = gen_code()
                    create_room(code, rn, un, auto_delete)
                    S.room_code     = code
                    S.username      = un
                    S.home_username = un
                    S.is_creator    = True   # definitely true — we just created it
                    S.page          = "chat"
                    S.show_code     = True
                    S.sent_media    = set()
                    st.rerun()
        with b2:
            if st.button("&#8592; BACK", use_container_width=True, key="cr_back"):
                S.page = "home"; st.rerun()
    with col2:
        st.markdown("""<div class="nx-box nx-box-c">
  <b style="color:#00D4FF;">Creator powers:</b><br><br>
  &middot; Share access code<br>
  &middot; Kick participants<br>
  &middot; Invite kicked users back<br>
  &middot; Lock / unlock room<br>
  &middot; Clear chat history<br>
  &middot; Delete room<br>
  &middot; Change auto-delete policy
</div>""", unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────────────────────
#  PAGE: JOIN ROOM
#  FIX #3: S.is_creator now derived from room data via _enter_room()
#  FIX #4: creator can bypass lock; state fully reset before entering
# ─────────────────────────────────────────────────────────────────────────────
def page_join():
    _style(); _guard()
    st.markdown('<div class="nx-panel"><div class="nx-ptitle">&#x229E; Join Existing Room</div></div>', unsafe_allow_html=True)
    col1, col2 = st.columns([2, 1])
    with col1:
        code_raw = st.text_input("Access Code", placeholder="e.g. AB3X9Z", max_chars=6, key="jn_code")
        username = st.text_input("Your Username", placeholder="e.g. Cipher_Y", max_chars=20, key="jn_user", value=S.home_username)
        _flash()
        b1, b2 = st.columns(2)
        with b1:
            if st.button("JOIN ROOM", type="primary", use_container_width=True, key="do_join"):
                err = _enter_room(code_raw, username)
                if err:
                    S.error = err
                st.rerun()
        with b2:
            if st.button("&#8592; BACK", use_container_width=True, key="jn_back"):
                S.page = "home"; st.rerun()
    with col2:
        st.markdown("""<div class="nx-box nx-box-a">
  <b style="color:#FFD700;">Before joining:</b><br><br>
  &middot; Get the 6-char code from creator<br>
  &middot; Pick a unique username<br>
  &middot; Messages carry your watermark<br>
  &middot; Creator can remove you<br>
  &middot; Save room to Friends after joining
</div>""", unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────────────────────
#  PAGE: CHAT ROOM
# ─────────────────────────────────────────────────────────────────────────────
def page_chat():
    _style(); _guard()

    code = S.room_code
    user = S.username

    # ── Guards ────────────────────────────────────────────────────────────
    if not code or not room_exists(code):
        st.markdown('<div class="nx-box nx-box-r">&#9888; This room no longer exists.</div>', unsafe_allow_html=True)
        if st.button("HOME", key="gh"): S.page = "home"; S.room_code = ""; st.rerun()
        return

    # FIX #3: Always re-sync is_creator from persistent room data on every render.
    # This ensures that if the creator re-enters via the join page (which previously
    # hard-coded is_creator=False), they immediately get their admin panel back.
    S.is_creator = is_creator(code, user)

    if is_kicked(code, user) and not S.is_creator:
        my_invites = [i for i in get_pending_invites(user) if i["room"] == code]
        if not my_invites:
            st.markdown('<div class="nx-box nx-box-r">&#9888; You were removed from this room. Ask the host to invite you back.</div>', unsafe_allow_html=True)
            if st.button("HOME", key="gk"): S.page = "home"; S.room_code = ""; st.rerun()
            return
        unkick_user(code, user); dismiss_invite(code, user)

    # ── Heartbeat (throttled) ──────────────────────────────────────────────
    now = time.time()
    if now - S.last_heartbeat > 1.0:
        heartbeat(code, user); S.last_heartbeat = now

    check_auto_delete(code)
    if HAS_AR: st_autorefresh(interval=2500, key="ar_chat")

    room   = get_room(code)
    msgs   = load_msgs(code)
    online = get_online(code)

    # ── Consume new media into session cache (recipients only) ────────────
    for m in msgs[-200:]:
        if m.get("k") == "media":
            mid = m["t"].replace("__MEDIA__", "").strip()
            if mid not in S.media_cache and mid not in S.sent_media:
                if media_exists(mid):
                    obj = consume_media(mid)
                    if obj: S.media_cache[mid] = obj

    # ── Code banner ───────────────────────────────────────────────────────
    if S.show_code:
        st.markdown(f"""
<div class="nx-success">
  <div class="nx-s-title">&#10003; ROOM CREATED &mdash; SHARE THIS CODE</div>
  <div style="font-family:'Exo 2',sans-serif;font-size:2.6rem;font-weight:800;
       letter-spacing:.5em;color:#00FFB3;padding:6px 0;">{h(code)}</div>
  <div style="font-family:'Share Tech Mono',monospace;font-size:.62rem;color:#3A5570;">
    Share this code with contacts. Lock the room once everyone joins.
  </div>
</div>""", unsafe_allow_html=True)
        if st.button("DISMISS", key="dismiss_code"): S.show_code = False; st.rerun()

    # ── Room header ───────────────────────────────────────────────────────
    adel      = room.get("auto_delete", "never")
    lock_lbl  = "LOCKED" if room.get("locked") else "OPEN"
    lock_cls  = "bd-r"   if room.get("locked") else "bd-g"
    role_lbl  = "ADMIN"  if S.is_creator        else "MEMBER"
    role_cls  = "bd-a"   if S.is_creator        else "bd-c"
    adel_short = {"immediate":"AUTO-DEL:5MIN","1h":"AUTO-DEL:1H","1d":"AUTO-DEL:1D"}
    adel_html  = f'<span class="bd bd-r">{adel_short[adel]}</span>' if adel != "never" else ""

    st.markdown(f"""
<div class="nx-rh">
  <div>
    <div class="nx-rname">{h(room.get("name","Chat Room"))}</div>
    <div class="nx-rmeta">
      <span>CODE:&nbsp;<b style="color:#00FFB3;letter-spacing:.22em;">{h(code)}</b></span>
      <span class="bd {lock_cls}">{lock_lbl}</span>
      {adel_html}
      <span class="bd bd-c">HOST: {h(room.get("creator","?"))}</span>
      <span class="bd {role_cls}">{role_lbl}</span>
    </div>
  </div>
  <div class="nx-rstat">
    <span style="color:#00FFB3;">&#9679;</span>&nbsp;{len(online)} online<br>
    <span style="font-size:.56rem;">{len(msgs)} messages</span>
  </div>
</div>""", unsafe_allow_html=True)

    chat_col, side_col = st.columns([3, 1])

    # ────────────────────────── CHAT COLUMN ──────────────────────────────
    with chat_col:
        components.html(
            render_messages(msgs, user, S.media_cache, S.sent_media, height=480),
            height=492, scrolling=False)

        # Locked means non-admins can't send; admin always can
        locked = bool(room.get("locked") and not S.is_creator)
        if locked:
            st.markdown('<div class="nx-warn" style="padding:6px 12px;margin:4px 0;">&#128274; Room locked &mdash; only the creator can send messages.</div>', unsafe_allow_html=True)

        # Text input row
        ic, bc = st.columns([5, 1])
        with ic:
            msg_text = st.text_input("msg", placeholder="Type a message…", key="msg_in", label_visibility="collapsed", disabled=locked)
        with bc:
            if st.button("SEND", type="primary", use_container_width=True, key="send_btn"):
                if not locked and msg_text.strip():
                    push_msg(code, user, msg_text.strip()); st.rerun()

        # Media upload
        if not locked:
            with st.expander("📎  Send One-Time Media"):
                st.markdown(
                    '<div style="font-family:\'Share Tech Mono\',monospace;font-size:.62rem;'
                    'color:#3A5570;margin-bottom:8px;line-height:1.9;">'
                    '&#8856; Recipient sees it <b style="color:#00FFB3;">ONCE</b> &mdash; then gone forever.<br>'
                    'Supported: JPG &middot; PNG &middot; GIF &middot; WEBP &middot; MP4 &middot; MOV &middot; WEBM'
                    '</div>',
                    unsafe_allow_html=True)
                upl = st.file_uploader(
                    "drop", type=["jpg","jpeg","png","gif","webp","mp4","mov","webm"],
                    key="media_upload", label_visibility="collapsed")
                if upl is not None:
                    st.markdown(f'<div style="font-family:\'Share Tech Mono\',monospace;font-size:.65rem;color:#00D4FF;padding:4px 0;">Selected: {h(upl.name)}</div>', unsafe_allow_html=True)
                    st.markdown("<div style='height:4px;'></div>", unsafe_allow_html=True)
                    if st.button("&#8853; SEND FILE", type="primary", key="smb", use_container_width=True):
                        raw  = upl.read()
                        b64  = base64.b64encode(raw).decode()
                        mime = upl.type or "application/octet-stream"
                        mid  = gen_media_id()
                        save_media(mid, b64, mime)
                        push_media_msg(code, user, mid)
                        S.sent_media.add(mid)
                        st.rerun()

        if not HAS_AR:
            if st.button("&#8635; REFRESH", key="mref"): st.rerun()

    # ────────────────────────── SIDEBAR ──────────────────────────────────
    with side_col:

        # Online users
        urows = ""
        for u in online:
            me   = (u == user)
            host = (u == room.get("creator"))
            ytag = ' <span class="bd bd-g" style="font-size:8px;padding:1px 4px;">YOU</span>'  if me   else ""
            htag = ' <span class="bd bd-a" style="font-size:8px;padding:1px 4px;">HOST</span>' if host else ""
            cc   = "#00FFB3" if me else "#8EB0CC"
            dot  = "&#9654;" if me else "&middot;"
            urows += f'<div class="nx-orow" style="color:{cc};">{dot}&nbsp;{h(u)}{ytag}{htag}</div>'

        if not urows:
            urows = '<div style="font-family:\'Share Tech Mono\',monospace;font-size:.62rem;color:#1A2F45;">&mdash; none &mdash;</div>'
        st.markdown(f'<div class="nx-panel"><div class="nx-ptitle">&#x2295; ONLINE ({len(online)})</div>{urows}</div>', unsafe_allow_html=True)

        # ── FIX #2: Add as friend — plain text placeholder, correct guard check ──
        others = [u for u in online if u != user]
        if others:
            # Use plain Unicode em-dash, NOT HTML entities — Streamlit selectbox
            # shows raw strings, it does NOT render HTML entities like &#8212;
            _PLACEHOLDER = "— select —"
            atgt = st.selectbox("Add as friend", [_PLACEHOLDER] + others, key="afs")
            if st.button("ADD FRIEND", use_container_width=True, key="afb"):
                if atgt != _PLACEHOLDER:
                    ok = save_friend(user, atgt, code)
                    S.success = f"✓ {atgt} added to friends!" if ok else f"{atgt} is already in your friends."
                    st.rerun()

        # Save room shortcut
        if not any(f.get("code") == code for f in load_friends(user)):
            if st.button("&#8853; SAVE ROOM", use_container_width=True, key="srb"):
                save_friend(user, f"Room:{room.get('name','?')}", code)
                S.success = "Room saved to friends!"; st.rerun()

        _flash()

        # Session card
        cdt       = datetime.fromtimestamp(room.get("created", time.time())).strftime("%H:%M")
        role_text = "CREATOR" if S.is_creator else "MEMBER"
        st.markdown(f"""
<div class="nx-sess">
  <div class="nx-sess-title">&#x229E; SESSION</div>
  <div class="nx-sess-row"><span>You</span>      <span class="nx-vg">{h(user)}</span></div>
  <div class="nx-sess-row"><span>Role</span>     <span class="nx-vc">{role_text}</span></div>
  <div class="nx-sess-row"><span>Started</span>  <span class="nx-vw">{cdt}</span></div>
  <div class="nx-sess-row"><span>Messages</span> <span class="nx-vw">{len(msgs)}</span></div>
  <div class="nx-sess-row"><span>Auto-del</span> <span class="nx-vw">{h(adel)}</span></div>
</div>""", unsafe_allow_html=True)

        st.markdown("<div style='margin-top:8px;'></div>", unsafe_allow_html=True)

        # FIX #4: on leave, fully reset all relevant session state
        if st.button("LEAVE ROOM", use_container_width=True, key="lrb"):
            push_msg(code, "SYSTEM", f"{user} left the room", "sys")
            leave_room(code, user)
            # Full reset so rejoining with any name/code works cleanly
            S.page         = "home"
            S.room_code    = ""
            S.username     = ""
            S.is_creator   = False
            S.sent_media   = set()
            S.media_cache  = {}
            S.confirm_del  = False
            S.confirm_clr  = False
            S.show_code    = False
            st.rerun()

        # ── Admin Controls ─────────────────────────────────────────────────
        if S.is_creator:
            st.markdown("<hr>", unsafe_allow_html=True)
            st.markdown('<div style="font-family:\'Share Tech Mono\',monospace;font-size:.58rem;letter-spacing:.18em;color:#FFD700;margin-bottom:8px;text-transform:uppercase;">&#9881; Admin Controls</div>', unsafe_allow_html=True)

            # Lock / unlock
            if room.get("locked"):
                if st.button("&#128275; UNLOCK ROOM", use_container_width=True, key="ulb"):
                    lock_room(code, False); push_msg(code,"SYSTEM","Room unlocked","sys"); st.rerun()
            else:
                if st.button("&#128274; LOCK ROOM", use_container_width=True, key="lkb"):
                    lock_room(code, True); push_msg(code,"SYSTEM","Room locked — only creator can send","sys"); st.rerun()

            # Clear chat
            if not S.confirm_clr:
                if st.button("CLEAR CHAT", use_container_width=True, key="ccb"):
                    S.confirm_clr = True; st.rerun()
            else:
                st.markdown('<div class="nx-warn" style="padding:6px 10px;margin:3px 0;">Clear all messages?</div>', unsafe_allow_html=True)
                cc1, cc2 = st.columns(2)
                with cc1:
                    if st.button("YES", use_container_width=True, key="ccy"):
                        clear_msgs_admin(code); S.confirm_clr = False; st.rerun()
                with cc2:
                    if st.button("NO", use_container_width=True, key="ccn"):
                        S.confirm_clr = False; st.rerun()

            # Kick user
            ok_to_kick = [u for u in online if u != user]
            if ok_to_kick:
                kt = st.selectbox("Kick user", ["— pick —"] + ok_to_kick, key="ks")
                if st.button("KICK USER", use_container_width=True, key="kb"):
                    if kt != "— pick —": kick_user(code, kt); st.rerun()

            # Invite back
            kicked_list = room.get("kicked", [])
            if kicked_list:
                st.markdown('<div style="font-family:\'Share Tech Mono\',monospace;font-size:.6rem;color:#3A5570;margin-top:8px;letter-spacing:.1em;text-transform:uppercase;">Invite Kicked User Back</div>', unsafe_allow_html=True)
                inv_target = st.selectbox("Select user", ["— pick —"] + kicked_list, key="inv_sel")
                if st.button("SEND INVITE", use_container_width=True, key="inv_btn"):
                    if inv_target != "— pick —":
                        send_invite(code, user, inv_target, room.get("name", code))
                        S.success = f"Invite sent to {inv_target}!"; st.rerun()

            # Auto-delete policy
            pols = list(_ADEL_LABELS.keys())
            ci   = pols.index(room.get("auto_delete","never"))
            np_  = st.selectbox("Auto-Delete Policy", pols, index=ci, format_func=lambda x: _ADEL_LABELS[x], key="adp")
            if st.button("APPLY POLICY", use_container_width=True, key="apb"):
                rd = load_rooms()
                if code in rd:
                    rd[code]["auto_delete"] = np_; rd[code]["last_cleared"] = 0; save_rooms(rd)
                    push_msg(code, "SYSTEM", f"Auto-delete policy: {np_}", "sys"); st.rerun()

            st.markdown("<div style='margin-top:4px;'></div>", unsafe_allow_html=True)

            # Delete room
            if not S.confirm_del:
                if st.button("DELETE ROOM", use_container_width=True, key="drb"):
                    S.confirm_del = True; st.rerun()
            else:
                st.markdown('<div class="nx-warn" style="padding:6px 10px;margin:3px 0;">Permanently delete this room?</div>', unsafe_allow_html=True)
                d1, d2 = st.columns(2)
                with d1:
                    if st.button("YES DELETE", use_container_width=True, key="dy"):
                        delete_room(code)
                        S.page = "home"; S.room_code = ""; S.username = ""
                        S.is_creator = False; S.confirm_del = False
                        S.sent_media = set(); S.media_cache = {}
                        st.rerun()
                with d2:
                    if st.button("CANCEL", use_container_width=True, key="dn"):
                        S.confirm_del = False; st.rerun()


# ─────────────────────────────────────────────────────────────────────────────
#  ROUTER
# ─────────────────────────────────────────────────────────────────────────────
def main():
    p = S.page
    if   p == "home":   page_home()
    elif p == "create": page_create()
    elif p == "join":   page_join()
    elif p == "chat":   page_chat()
    else: S.page = "home"; st.rerun()

main()
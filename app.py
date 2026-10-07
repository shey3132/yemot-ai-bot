import os
import time
import re
import json
import sqlite3
import sys
import traceback
import atexit
import random
import string
from collections import defaultdict
from contextlib import closing
from threading import Lock
from concurrent.futures import ThreadPoolExecutor

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from flask import Flask, request, Response, send_file, jsonify
from google import genai
from google.genai import types
import psycopg2
from psycopg2.extras import RealDictCursor

app = Flask(__name__)

# --- הגדרות סביבה ---
YEMOT_TOKEN        = os.environ.get("YEMOT_TOKEN")
GEMINI_API_KEY     = os.environ.get("GEMINI_API_KEY")
GEMINI_API_KEY_2   = os.environ.get("GEMINI_API_KEY_2")
GROQ_API_KEY       = os.environ.get("GROQ_API_KEY")
GOOGLE_SCRIPT_URL  = os.environ.get("GOOGLE_SCRIPT_URL")
TARGET_EMAIL       = os.environ.get("TARGET_EMAIL")
DATABASE_URL       = os.environ.get("DATABASE_URL")

MODEL_NAME         = "gemini-2.5-flash"
GROQ_CHAT_MODEL    = os.environ.get("GROQ_CHAT_MODEL", "openai/gpt-oss-20b")  # llama-3.1-8b-instant הושבת ב-Groq ב-16.8.2026
GROQ_WHISPER_MODEL = os.environ.get("GROQ_WHISPER_MODEL", "whisper-large-v3")  # המדויק יותר; אפשר להחליף ל-whisper-large-v3-turbo דרך משתנה סביבה
MAX_TOOL_ROUNDS     = 2  # כמה פעמים Gemini רשאי לבקש חיפוש בוויקיפדיה בשיחה אחת
ADMIN_HTML_FILE    = "admin.html"
MAX_HISTORY_MESSAGES = 12   # כמה הודעות אחרונות נשלחות למודל (6 סבבי שאלה ותשובה), כדי שהשיחה לא תתארך ותאט
MAX_SILENT_RETRIES   = 2    # כמה פעמים ברצף מבקשים מהמתקשר לחזור כששתק, לפני שנפרדים ומנתקים
GEMINI_TIMEOUT_MS    = 10000  # זמן מקסימלי לבקשה אחת ל-Gemini, כדי שתקלה שלו לא תתלה את השיחה
GEMINI_BUDGET_SEC    = 20     # אחרי כמה שניות מוותרים על שאר מפתחות Gemini ועוברים ל-Groq

auth_codes     = {}
active_sessions = {}

def log_event(call_id, event_name, **kwargs):
    log_data = {"call_id": call_id, "event": event_name, "timestamp": time.time()}
    log_data.update(kwargs)
    print(json.dumps(log_data), flush=True)

session = requests.Session()
retry_strategy = Retry(total=3, backoff_factor=0.3, status_forcelist=[429, 500, 502, 503, 504])
adapter = HTTPAdapter(max_retries=retry_strategy)
session.mount("http://", adapter)
session.mount("https://", adapter)

RECORD_COMMAND = "user_audio,no,record,,,yes,yes,no,1,120"
search_cache   = {}
executor       = ThreadPoolExecutor(max_workers=4)
query_locks    = defaultdict(Lock)

# ── PostgreSQL ──────────────────────────────────────────────
def get_db():
    return psycopg2.connect(DATABASE_URL, sslmode='require', cursor_factory=RealDictCursor)

def init_db():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute('''
                CREATE TABLE IF NOT EXISTS conversations (
                    caller_id TEXT PRIMARY KEY,
                    name TEXT,
                    history TEXT
                )
            ''')
            cur.execute('''
                CREATE TABLE IF NOT EXISTS allowed_numbers (
                    phone TEXT PRIMARY KEY,
                    label TEXT DEFAULT ''
                )
            ''')
            cur.execute('''
                CREATE TABLE IF NOT EXISTS api_stats (
                    id SERIAL PRIMARY KEY,
                    ts TIMESTAMPTZ DEFAULT NOW(),
                    call_id TEXT,
                    api_key_index INTEGER,
                    api_name TEXT,
                    success BOOLEAN
                )
            ''')
            cur.execute('''
                CREATE TABLE IF NOT EXISTS call_state (
                    call_id TEXT PRIMARY KEY,
                    silent INTEGER DEFAULT 0,
                    ts TIMESTAMPTZ DEFAULT NOW()
                )
            ''')
            cur.execute('''
                CREATE TABLE IF NOT EXISTS call_log (
                    id SERIAL PRIMARY KEY,
                    ts TIMESTAMPTZ DEFAULT NOW(),
                    caller_id TEXT,
                    call_id TEXT
                )
            ''')
        conn.commit()

init_db()

# ── נתוני שיחה ──────────────────────────────────────────────
def get_chat_data(caller_id):
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT history, name FROM conversations WHERE caller_id=%s", (caller_id,))
                row = cur.fetchone()
                if row:
                    history = json.loads(row['history']) if row['history'] else []
                    return history, row['name']
    except Exception as e:
        log_event(caller_id, "db_get_error", error=str(e))
    return [], None

def save_chat_data(caller_id, history, name):
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute('''
                    INSERT INTO conversations (caller_id, history, name) VALUES (%s,%s,%s)
                    ON CONFLICT (caller_id) DO UPDATE
                    SET history=EXCLUDED.history,
                        name=COALESCE(EXCLUDED.name, conversations.name)
                ''', (caller_id, json.dumps(history[-50:]), name))
            conn.commit()
    except Exception as e:
        log_event(caller_id, "db_save_error", error=str(e))

def delete_chat_data(caller_id):
    """מוחק את היסטוריית השיחה אבל משאיר את השם, כדי שבשיחה הבאה נועם יברך בשם."""
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE conversations SET history=NULL WHERE caller_id=%s", (caller_id,))
            conn.commit()
    except Exception as e:
        log_event(caller_id, "db_delete_error", error=str(e))

# ── מספרים מורשים ───────────────────────────────────────────
def load_allowed_numbers():
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT phone FROM allowed_numbers")
                return [r['phone'] for r in cur.fetchall()]
    except:
        return []

def is_allowed(caller_id):
    return caller_id in load_allowed_numbers()

# ── סטטיסטיקות ──────────────────────────────────────────────
def log_api_stat(call_id, api_name, key_index, success):
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO api_stats (call_id, api_name, api_key_index, success) VALUES (%s,%s,%s,%s)",
                    (call_id, api_name, key_index, success)
                )
            conn.commit()
    except:
        pass

def log_call(caller_id, call_id):
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO call_log (caller_id, call_id) VALUES (%s,%s)", (caller_id, call_id))
            conn.commit()
    except:
        pass

def call_already_greeted(call_id):
    """האם כבר בירכנו בשיחה הזו. בקשה בלי הקלטה אחרי ברכה פירושה שהמתקשר שתק."""
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM call_log WHERE call_id=%s LIMIT 1", (call_id,))
                return cur.fetchone() is not None
    except Exception as e:
        log_event(call_id, "db_greeted_check_error", error=str(e))
        return False

def bump_silence(call_id):
    """סופר שתיקות ברצף בשיחה. אם ה-DB לא זמין מחזיר 1, כדי שלא ננתק בטעות."""
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM call_state WHERE ts < NOW() - INTERVAL '1 day'")
                cur.execute('''
                    INSERT INTO call_state (call_id, silent) VALUES (%s, 1)
                    ON CONFLICT (call_id) DO UPDATE SET silent = call_state.silent + 1, ts = NOW()
                    RETURNING silent
                ''', (call_id,))
                n = cur.fetchone()['silent']
            conn.commit()
            return n
    except Exception as e:
        log_event(call_id, "db_silence_error", error=str(e))
        return 1

def reset_silence(call_id):
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM call_state WHERE call_id=%s", (call_id,))
            conn.commit()
    except Exception:
        pass

# ── עזרים ───────────────────────────────────────────────────
class CallError(Exception):
    """שגיאה עם סוג, כדי שהמתקשר ישמע הודעה שמתאימה למה שקרה."""
    def __init__(self, kind, detail=""):
        super().__init__(detail or kind)
        self.kind = kind

ERROR_MESSAGES = {
    "download":   "לא הצלחתי לקבל את ההקלטה אנא נסו שוב",
    "busy":       "המערכת עמוסה כרגע אנא נסו שוב",
    "connection": "החיבור לשרת נתקע אנא נסו שוב",
    "generic":    "חלה שגיאה זמנית אנא נסו שוב",
}

def error_kind(e):
    if isinstance(e, CallError):
        return e.kind
    err = str(e).lower()
    if "exhausted" in err or "429" in err:
        return "busy"
    if "timeout" in err or "connection" in err:
        return "connection"
    return "generic"

def hangup_response(text):
    return Response(f"read=t-{clean_text(text)}=hangup", mimetype='text/plain')

def record_response(text):
    return Response(f"read=t-{clean_text(text)}={RECORD_COMMAND}", mimetype='text/plain')

def trim_history(history):
    """משאיר רק את ההודעות האחרונות, ומוודא שההיסטוריה מתחילה בהודעת משתמש."""
    h = history[-MAX_HISTORY_MESSAGES:]
    while h and h[0].get("role") != "user":
        h = h[1:]
    return h

def clean_text(text):
    if not text: return ""
    text = re.sub(r'[\.\-\=&,\?!:;_\(\)\[\]\{\}\"\']', ' ', text)
    text = re.sub(r'[^\u0590-\u05FFa-zA-Z0-9\s]', '', text)
    return " ".join(text.split())

WIKI_HEADERS = {"User-Agent": "YemotAIBot/1.0 (https://github.com/shey3132/yemot-ai-bot)"}

def perform_wikipedia_search(call_id, query):
    query = re.sub(r'[^\u0590-\u05FFa-zA-Z0-9\s]', ' ', query).strip()
    log_event(call_id, "wikipedia_search", query=query)
    if not query: return "לא צוין מושג תקין לחיפוש"
    with query_locks[query]:
        if query in search_cache: return search_cache[query]['result']
        res = None
        try:
            res = session.get("https://he.wikipedia.org/w/api.php", params={"action":"query","list":"search","srsearch":query,"format":"json","srlimit":1}, headers=WIKI_HEADERS, timeout=10)
            res.raise_for_status()
            data = res.json().get("query", {}).get("search", [])
            if not data: return "לא נמצא מידע"
            title = data[0]["title"]
            res = session.get("https://he.wikipedia.org/w/api.php", params={"action":"query","prop":"extracts","exintro":True,"explaintext":True,"titles":title,"format":"json"}, headers=WIKI_HEADERS, timeout=10)
            res.raise_for_status()
            pages = res.json().get("query", {}).get("pages", {})
            page_id = list(pages.keys())[0]
            extract = pages[page_id].get("extract", "")[:600]
            result = clean_text(f"ויקיפדיה על {title} {extract}")
            search_cache[query] = {'result': result, 'time': time.time()}
            return result
        except Exception as e:
            log_event(call_id, "wikipedia_error", error=str(e),
                      status=(res.status_code if res is not None else None),
                      body=(res.text[:120] if res is not None else ""))
            return "תקלה בחיפוש בויקיפדיה"

def wikipedia_search(query: str) -> str:
    """Search Wikipedia to get accurate information about terms, people or events."""
    return query

def remember_name(name: str) -> str:
    """Save the caller's first name, only when the caller clearly says their own name."""
    return name

def end_call() -> str:
    """Call this when the caller says goodbye or thanks and has nothing more to ask. After it returns, say a very short goodbye."""
    return "ok"

AUDIO_WORDS  = ("שמע", "אודיו", "הקלט", "audio")
REFUSE_WORDS = ("לא יכול", "לא מסוגל", "אינני יכול", "אינני מסוגל", "אין לי יכולת", "מצטער", "cannot", "can't")

def looks_like_audio_refusal(text):
    """מזהה תשובה של Gemini בסגנון 'אני לא יכול לענות על קבצי שמע'."""
    t = text or ""
    return any(w in t for w in AUDIO_WORDS) and any(w in t for w in REFUSE_WORDS)

def drop_refusals_from_history(history):
    """מסיר מההיסטוריה סירובים כאלה (ואת השאלה שלפניהם), כדי שהמודל לא ימשיך לחקות אותם."""
    out = []
    for h in history:
        if h.get("role") == "assistant" and looks_like_audio_refusal(h.get("content")):
            if out and out[-1].get("role") == "user":
                out.pop()
            continue
        out.append(h)
    return out

BIDI_CHARS = {ord(c): None for c in "\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"}

def clean_transcript(text):
    """מנקה תמלול Whisper: מסיר תווי כיווניות נסתרים, ומקצר לולאות שחוזרות 3 פעמים ויותר ('X X X' -> 'X')."""
    words = (text or "").translate(BIDI_CHARS).split()
    norm  = [re.sub(r"[^\w]", "", w) for w in words]
    out, i = [], 0
    while i < len(words):
        collapsed = False
        for n in range(1, 9):
            block = norm[i:i + n]
            if len(block) < n or not any(block):
                continue
            reps = 1
            while norm[i + reps * n: i + (reps + 1) * n] == block:
                reps += 1
            if reps >= 3:
                out.extend(words[i:i + n]); i += reps * n; collapsed = True
                break
        if not collapsed:
            out.append(words[i]); i += 1
    return " ".join(out).strip()

def transcribe_audio(call_id, audio_bytes):
    """תמלול עברית דרך Groq Whisper. מחזיר מחרוזת ריקה אם נכשל — לא זורק שגיאה."""
    if not GROQ_API_KEY:
        return ""
    try:
        r = session.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            files={"file": ("audio.wav", audio_bytes, "audio/wav")},
            data={"model": GROQ_WHISPER_MODEL, "language": "he", "temperature": "0"},
            timeout=8
        )
        r.raise_for_status()
        return clean_transcript(r.json().get("text") or "")
    except Exception as e:
        log_event(call_id, "transcribe_error", error=str(e))
        return ""

# ══════════════════════════════════════════════════════════════
# Admin routes
# ══════════════════════════════════════════════════════════════

@app.route('/health', methods=['GET'])
def health():
    # בדיקת חיים קלה (בלי DB), כדי ששירות פינג חיצוני ימנע מ-Render להירדם
    return "ok", 200

@app.route('/admin', methods=['GET'])
def admin_page():
    return send_file(ADMIN_HTML_FILE)

@app.route('/admin/send-code', methods=['POST'])
def admin_send_code():
    data  = request.get_json(force=True)
    email = (data.get('email') or '').strip().lower()
    if not email or '@' not in email:
        return jsonify({"ok": False, "error": "כתובת מייל לא תקינה"})
    if not TARGET_EMAIL or email != TARGET_EMAIL.strip().lower():
        return jsonify({"ok": False, "error": "כתובת המייל אינה מורשית"})
    if not GOOGLE_SCRIPT_URL:
        return jsonify({"ok": False, "error": "GOOGLE_SCRIPT_URL לא מוגדר"})

    code = ''.join(random.choices(string.digits, k=6))
    auth_codes[email] = (code, time.time() + 300)

    body = f"""
    <!DOCTYPE html>
    <html dir="rtl" lang="he">
    <head><meta charset="UTF-8"></head>
    <body style="margin:0;padding:0;background:#0f1117;font-family:Arial,sans-serif;">
      <table width="100%" cellpadding="0" cellspacing="0" style="background:#0f1117;padding:40px 0;">
        <tr><td align="center">
          <table width="480" cellpadding="0" cellspacing="0" style="background:#1a1d27;border-radius:16px;border:1px solid rgba(255,255,255,0.08);overflow:hidden;">
            <tr>
              <td style="background:#5b6ef5;padding:24px 32px;text-align:center;">
                <span style="font-size:28px;">🔐</span>
                <h1 style="color:#fff;margin:8px 0 0;font-size:20px;font-weight:600;">נועם — קוד אימות</h1>
              </td>
            </tr>
            <tr>
              <td style="padding:32px;text-align:center;">
                <p style="color:#7b82a8;font-size:14px;margin:0 0 24px;">הקוד שלך לכניסה לדף הניהול (לחיצה כפולה על המספר כדי להעתיק):</p>
                <div style="background:#0f1117;border:1px solid rgba(91,110,245,0.4);border-radius:12px;padding:20px 32px;display:inline-block;margin-bottom:24px;user-select:all;">
                  <span style="font-size:42px;font-weight:700;letter-spacing:14px;color:#5b6ef5;font-family:monospace;">{code}</span>
                </div>
                <p style="color:#4a5175;font-size:12px;margin:0;">הקוד תקף ל-5 דקות בלבד</p>
              </td>
            </tr>
            <tr>
              <td style="padding:16px 32px;border-top:1px solid rgba(255,255,255,0.06);text-align:center;">
                <p style="color:#4a5175;font-size:11px;margin:0;">נשלח אוטומטית ממערכת נועם העוזר הקולי</p>
              </td>
            </tr>
          </table>
        </td></tr>
      </table>
    </body>
    </html>
    """
    try:
        session.post(GOOGLE_SCRIPT_URL, json={"to": email, "subject": "קוד אימות — נועם ניהול", "htmlBody": body}, timeout=10)
    except Exception as e:
        return jsonify({"ok": False, "error": "שגיאה בשליחת המייל"})

    return jsonify({"ok": True})

@app.route('/admin/verify-code', methods=['POST'])
def admin_verify_code():
    data  = request.get_json(force=True)
    email = (data.get('email') or '').strip().lower()
    code  = (data.get('code')  or '').strip()
    entry = auth_codes.get(email)
    if not entry:
        return jsonify({"ok": False, "error": "לא נשלח קוד לכתובת זו"})
    saved_code, expires_at = entry
    if time.time() > expires_at:
        del auth_codes[email]
        return jsonify({"ok": False, "error": "הקוד פג תוקף"})
    if code != saved_code:
        return jsonify({"ok": False, "error": "קוד שגוי"})
    del auth_codes[email]
    token = ''.join(random.choices(string.ascii_letters + string.digits, k=48))
    active_sessions[token] = email
    return jsonify({"ok": True, "token": token})

def require_session():
    return active_sessions.get(request.headers.get('X-Token', ''))

@app.route('/admin/numbers', methods=['GET'])
def admin_get_numbers():
    if not require_session():
        return jsonify({"ok": False, "error": "לא מורשה"}), 401
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT phone, label FROM allowed_numbers ORDER BY phone")
                rows = cur.fetchall()
        return jsonify({"ok": True, "numbers": [{"phone": r['phone'], "label": r['label'] or ''} for r in rows]})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route('/admin/numbers', methods=['POST'])
def admin_save_numbers():
    if not require_session():
        return jsonify({"ok": False, "error": "לא מורשה"}), 401
    data = request.get_json(force=True)
    raw  = data.get('numbers')
    if not isinstance(raw, list):
        return jsonify({"ok": False, "error": "פורמט לא תקין"}), 400
    numbers = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        phone = str(item.get('phone', '')).strip()
        label = str(item.get('label', '')).strip()
        if phone:
            numbers.append((phone, label))
    # הגנה: רשימה ריקה נשמרת רק באישור מפורש מהלקוח (מונע מחיקת כל המספרים בטעות)
    if not numbers and data.get('confirm_empty') is not True:
        return jsonify({"ok": False, "error": "הרשימה ריקה — לא נשמר כדי למנוע מחיקה בטעות"}), 400
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM allowed_numbers")
                for phone, label in numbers:
                    cur.execute("INSERT INTO allowed_numbers (phone, label) VALUES (%s,%s) ON CONFLICT DO NOTHING", (phone, label))
            conn.commit()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route('/admin/stats', methods=['GET'])
def admin_stats():
    if not require_session():
        return jsonify({"ok": False, "error": "לא מורשה"}), 401
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                # שיחות היום
                cur.execute("SELECT COUNT(*) AS c FROM call_log WHERE ts > NOW() - INTERVAL '1 day'")
                today = cur.fetchone()['c']
                # שיחות השבוע
                cur.execute("SELECT COUNT(*) AS c FROM call_log WHERE ts > NOW() - INTERVAL '7 days'")
                week = cur.fetchone()['c']
                # שיחות סה"כ
                cur.execute("SELECT COUNT(*) AS c FROM call_log")
                total = cur.fetchone()['c']
                # גרף 7 ימים
                cur.execute("""
                    SELECT DATE(ts AT TIME ZONE 'Asia/Jerusalem') AS day, COUNT(*) AS c
                    FROM call_log WHERE ts > NOW() - INTERVAL '7 days'
                    GROUP BY day ORDER BY day
                """)
                chart = [{"day": str(r['day']), "count": r['c']} for r in cur.fetchall()]
                # סטטוס מפתחות — 100 בקשות אחרונות
                cur.execute("""
                    SELECT api_name, api_key_index,
                           COUNT(*) AS total,
                           SUM(CASE WHEN success THEN 1 ELSE 0 END) AS ok
                    FROM api_stats
                    WHERE ts > NOW() - INTERVAL '1 hour'
                    GROUP BY api_name, api_key_index
                    ORDER BY api_name, api_key_index
                """)
                keys = [{"api": r['api_name'], "index": r['api_key_index'],
                         "total": r['total'], "ok": r['ok']} for r in cur.fetchall()]
        return jsonify({"ok": True, "today": today, "week": week, "total": total, "chart": chart, "keys": keys})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

# ══════════════════════════════════════════════════════════════
# נתיב ראשי — שיחות
# ══════════════════════════════════════════════════════════════

def silence_response(call_id):
    """המתקשר שתק: מבקשים שיחזור, ואחרי כמה שתיקות ברצף נפרדים ומנתקים."""
    n = bump_silence(call_id)
    log_event(call_id, "caller_silent", count=n)
    if n > MAX_SILENT_RETRIES:
        return hangup_response("לא שמעתי אתכם תתקשרו שוב בכל עת להתראות")
    return record_response("לא שמעתי אתכם אנא דברו לאחר הצליל")

@app.route('/ai-chat', methods=['GET', 'POST'])
def ai_chat():
    caller_id = request.values.get('ApiPhone', 'unknown')
    call_id   = request.values.get('ApiCallId', 'unknown_call')

    log_event(call_id, "incoming_call_request", params=dict(request.values))

    if not is_allowed(caller_id):
        log_event(call_id, "unauthorized_caller", caller_id=caller_id)
        return hangup_response("מצטערים השירות אינו זמין עבורך לקבלת גישה אנא פנו למנהל השירות")

    history, known_name = get_chat_data(caller_id)

    if request.values.get('hangup') == 'yes':
        log_event(call_id, "hangup_received")
        delete_chat_data(caller_id)
        executor.submit(reset_silence, call_id)
        return Response("noop", mimetype='text/plain')

    audio_path = request.values.getlist('user_audio')
    if not audio_path:
        if call_already_greeted(call_id):
            return silence_response(call_id)
        log_event(call_id, "first_greeting_prompt")
        log_call(caller_id, call_id)
        if known_name:
            return record_response(f"שלום {known_name} כאן נועם אנא דברו לאחר הצליל")
        return record_response("שלום כאן נועם איך קוראים לכם ובמה אפשר לעזור אנא דברו לאחר הצליל")

    try:
        try:
            audio_res = session.get("https://www.call2all.co.il/ym/api/DownloadFile",
                                    params={"token": YEMOT_TOKEN, "path": f"ivr2:{audio_path[-1]}"},
                                    timeout=20)
            audio_res.raise_for_status()
        except requests.RequestException as dl_err:
            raise CallError("download", str(dl_err))

        content_type = audio_res.headers.get('Content-Type', '').lower()
        if 'text' in content_type or 'html' in content_type:
            raise CallError("download", "Downloaded file is not audio")
        if len(audio_res.content) < 1000:
            log_event(call_id, "audio_too_short", size=len(audio_res.content))
            return silence_response(call_id)
        executor.submit(reset_silence, call_id)

        if known_name:
            name_rule = f"The caller's name is {known_name}. Use it only now and then, naturally. "
        else:
            name_rule = ("The caller's name is not known yet. If the caller says their own name, "
                         "call the remember_name tool once and greet them warmly by name. "
                         "Never ask for the name again after the first question. ")
        system_prompt = (
            "You are Noam, a helpful, polite and friendly voice assistant on a phone call. "
            "The user speaks Hebrew. Their message arrives either as a voice recording or as its transcript. "
            "You CAN understand spoken audio. Never say that you cannot process audio or audio files. "
            + name_rule +
            "CRITICAL RULE: Keep your answers VERY SHORT, concise, and conversational. "
            "Respond in 1 to 3 short sentences MAXIMUM per answer. "
            "Only when the caller explicitly asks you to elaborate, explain more or give details, "
            "you may answer in up to 6 short sentences. "
            "If the recording is silent, unclear or you did not understand it, ask briefly to repeat. Never guess what was said. "
            "When asked about a person, place, event or any fact you are not completely sure about, "
            "call the wikipedia_search tool first and answer from its result. "
            "If nothing is found, say briefly that you do not know. Never invent facts. "
            "Never promise to get back to the user later and never say that you are still searching. "
            "Answer in this same reply, or say briefly that you do not know. "
            "When the caller says goodbye or thanks and asks for nothing more, call the end_call tool, "
            "then reply with a very short warm goodbye. "
            "FORMAT RULE: Do NOT use any punctuation marks whatsoever. "
            "Write numbers as Hebrew words, not digits. "
            "Use only clear Hebrew letters and spaces. Never output English, emoji or internal thoughts."
        )

        # Gemini שומע את ההקלטה עצמה. Whisper רץ במקביל, בלי לעכב, ומשמש רק כגיבוי
        # (אם Gemini מסרב להבין שמע, או אם הוא נכשל ועוברים ל-Groq) ולשמירת ההיסטוריה.
        transcript_future = executor.submit(transcribe_audio, call_id, audio_res.content)

        def get_transcript(timeout):
            try:
                return transcript_future.result(timeout=timeout)
            except Exception:
                return ""

        history = trim_history(drop_refusals_from_history(history))
        history_contents = [types.Content(role='user' if h['role'] == 'user' else 'model',
                                          parts=[types.Part(text=h['content'])]) for h in history]
        AUDIO_INSTRUCTION = "הקשב להקלטה המצורפת וענה למשתמש בעברית תשובה קצרה מאוד של עד שלושה משפטים וללא סימני פיסוק כלל."

        def build_contents(send_audio):
            if send_audio:
                parts = [types.Part.from_bytes(data=audio_res.content, mime_type="audio/wav"),
                         types.Part(text=AUDIO_INSTRUCTION)]
            else:
                parts = [types.Part(text=get_transcript(8))]
            return history_contents + [types.Content(role="user", parts=parts)]

        send_audio    = True
        audio_refused = False

        gemini_keys   = [k for k in [GEMINI_API_KEY, GEMINI_API_KEY_2] if k]
        response_text = None

        attempts = list(enumerate(gemini_keys))
        attempt_no = 0
        gemini_started = time.time()
        ending = False
        while attempt_no < len(attempts):
            if time.time() - gemini_started > GEMINI_BUDGET_SEC:
                log_event(call_id, "gemini_budget_exceeded", seconds=round(time.time() - gemini_started, 1))
                break
            idx, current_key = attempts[attempt_no]
            attempt_no += 1
            ending = False
            try:
                local_client = genai.Client(api_key=current_key,
                                            http_options=types.HttpOptions(timeout=GEMINI_TIMEOUT_MS))
                key_contents = build_contents(send_audio)  # רשימה חדשה לכל ניסיון, כדי שכשל לא ילכלך את הבא
                log_event(call_id, "gemini_attempt", key=idx + 1, mode="audio" if send_audio else "text")
                response = None

                for round_no in range(MAX_TOOL_ROUNDS + 1):
                    allow_tools = round_no < MAX_TOOL_ROUNDS
                    config = types.GenerateContentConfig(
                        system_instruction=system_prompt,
                        tools=[wikipedia_search, remember_name, end_call],
                        # בלי זה ה-SDK מריץ את wikipedia_search בעצמו (שרק מחזירה את השאלה) ומדלג על החיפוש האמיתי
                        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                        # בסבב האחרון אוסרים קריאה לכלים, כדי שנקבל תשובת טקסט
                        tool_config=None if allow_tools else types.ToolConfig(
                            function_calling_config=types.FunctionCallingConfig(mode="NONE")
                        ),
                    )
                    response = local_client.models.generate_content(
                        model=MODEL_NAME, contents=key_contents, config=config
                    )
                    if not response.function_calls:
                        break

                    key_contents.append(response.candidates[0].content)
                    parts = []
                    for call in response.function_calls:
                        args = call.args or {}
                        if call.name == "wikipedia_search":
                            res = perform_wikipedia_search(call_id, args.get("query", ""))
                        elif call.name == "remember_name":
                            new_name = clean_text(str(args.get("name", "")))[:30]
                            if new_name:
                                known_name = new_name
                            log_event(call_id, "name_remembered", name=new_name)
                            res = "saved"
                        elif call.name == "end_call":
                            ending = True
                            res = "ok now say a very short goodbye"
                        else:
                            res = "unknown tool"
                        parts.append(types.Part.from_function_response(
                            name=call.name, response={"result": res}
                        ))
                    key_contents.append(types.Content(role="user", parts=parts))

                temp_text = response.text or ""
                if not temp_text.strip():
                    raise Exception("Gemini returned empty text")
                if "מצטער" in temp_text and "להקשיב" in temp_text:
                    raise Exception("Gemini hallucinated refusal")
                if send_audio and looks_like_audio_refusal(temp_text):
                    if get_transcript(8):
                        send_audio = False     # בניסיון הבא נשלח רק את התמלול
                        audio_refused = True
                    raise Exception("Gemini refused audio: " + temp_text[:80])
                response_text = response.text
                log_api_stat(call_id, "gemini", idx + 1, True)
                log_event(call_id, f"gemini_key_{idx+1}_success")
                break
            except Exception as gemini_err:
                log_api_stat(call_id, "gemini", idx + 1, False)
                log_event(call_id, f"gemini_key_{idx+1}_failed", error=str(gemini_err))
                if audio_refused:
                    audio_refused = False
                    attempts.insert(attempt_no, (idx, current_key))  # אותו מפתח שוב, הפעם עם תמלול בלבד
                continue

        if not response_text:
            if GROQ_API_KEY:
                try:
                    user_transcription = get_transcript(15)
                    if not user_transcription:
                        raise Exception("Whisper transcription failed or empty")

                    chat_res = session.post(
                        "https://api.groq.com/openai/v1/chat/completions",
                        headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
                        json={"model": GROQ_CHAT_MODEL, "messages": [
                            {"role": "system", "content": system_prompt},
                            *[{"role": h['role'], "content": h['content']} for h in history],
                            {"role": "user", "content": user_transcription}
                        ]},
                        timeout=15
                    )
                    chat_res.raise_for_status()
                    response_text = chat_res.json()['choices'][0]['message']['content']
                    log_api_stat(call_id, "groq", 1, True)
                except Exception as groq_err:
                    body = ""
                    try:
                        body = groq_err.response.text[:200]
                    except Exception:
                        pass
                    log_api_stat(call_id, "groq", 1, False)
                    log_event(call_id, "groq_failed", error=str(groq_err), body=body)
                    raise Exception("All APIs exhausted")
            else:
                raise Exception("Gemini failed and no Groq key")

        ai_reply = clean_text(response_text)
        transcript = get_transcript(3)
        log_event(call_id, "user_said", text=transcript)
        user_content_for_history = transcript or "[קובץ שמע]"
        log_event(call_id, "bot_reply", text=ai_reply)
        history.extend([{"role": "user", "content": user_content_for_history},
                         {"role": "assistant", "content": ai_reply}])
        if ending:
            save_chat_data(caller_id, [], known_name)  # מנקה היסטוריה, שומר שם
            log_event(call_id, "call_ended_by_assistant")
            return hangup_response(ai_reply)
        save_chat_data(caller_id, history, known_name)
        return record_response(ai_reply)

    except Exception as e:
        traceback.print_exc(file=sys.stderr)
        log_event(call_id, "global_exception", error=str(e))
        # חשוב: ממשיכים לפקודת ההקלטה, אחרת ימות המשיח מנתקים את השיחה אחרי ההודעה
        return record_response(ERROR_MESSAGES[error_kind(e)])

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)

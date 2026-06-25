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

app = Flask(__name__)

# --- הגדרות סביבה ---
YEMOT_TOKEN        = os.environ.get("YEMOT_TOKEN")
GEMINI_API_KEY     = os.environ.get("GEMINI_API_KEY")
GEMINI_API_KEY_2   = os.environ.get("GEMINI_API_KEY_2")
GROQ_API_KEY       = os.environ.get("GROQ_API_KEY")
GOOGLE_SCRIPT_URL  = os.environ.get("GOOGLE_SCRIPT_URL")   # לשליחת קוד אימות בלבד
TARGET_EMAIL       = os.environ.get("TARGET_EMAIL")         # המייל שיקבל קוד אימות

# מודלים
MODEL_NAME        = "gemini-2.5-flash"
GROQ_CHAT_MODEL   = "llama-3.1-8b-instant"
GROQ_WHISPER_MODEL = "whisper-large-v3-turbo"

# קבצים
ALLOWED_NUMBERS_FILE = "allowed_numbers.txt"
ADMIN_HTML_FILE      = "admin.html"

# --- אחסון קודי אימות זמניים: {email: (code, expires_at)} ---
auth_codes = {}
# --- סשנים פעילים: {token: email} ---
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
DB_FILE = "chat_memory.db"
search_cache = {}
query_locks = defaultdict(Lock)

def init_db():
    with closing(sqlite3.connect(DB_FILE, timeout=30)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        with conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS conversations (
                    caller_id TEXT PRIMARY KEY,
                    name TEXT,
                    history TEXT
                )
            ''')
init_db()

def load_allowed_numbers():
    try:
        with open(ALLOWED_NUMBERS_FILE, 'r', encoding='utf-8') as f:
            return [line.strip() for line in f if line.strip()]
    except FileNotFoundError:
        return []

def save_allowed_numbers(numbers):
    with open(ALLOWED_NUMBERS_FILE, 'w', encoding='utf-8') as f:
        f.write('\n'.join(numbers))

def is_allowed(caller_id):
    return caller_id in load_allowed_numbers()

def get_chat_data(caller_id):
    try:
        with closing(sqlite3.connect(DB_FILE, timeout=30)) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT history, name FROM conversations WHERE caller_id = ?", (caller_id,))
            row = cursor.fetchone()
            if row and row[0]:
                return json.loads(row[0]), row[1]
    except Exception as e:
        log_event(caller_id, "db_get_error", error=str(e))
    return [], None

def save_chat_data(caller_id, history, name):
    try:
        history_to_save = history[-50:]
        with closing(sqlite3.connect(DB_FILE, timeout=30)) as conn:
            with conn:
                conn.execute('''
                    INSERT INTO conversations (caller_id, history, name)
                    VALUES (?, ?, ?)
                    ON CONFLICT(caller_id)
                    DO UPDATE SET history=excluded.history,
                    name=COALESCE(excluded.name, conversations.name)
                ''', (caller_id, json.dumps(history_to_save), name))
    except Exception as e:
        log_event(caller_id, "db_save_error", error=str(e))

def delete_chat_data(caller_id):
    try:
        with closing(sqlite3.connect(DB_FILE, timeout=30)) as conn:
            with conn:
                conn.execute("DELETE FROM conversations WHERE caller_id = ?", (caller_id,))
    except Exception as e:
        log_event(caller_id, "db_delete_error", error=str(e))

def clean_text(text):
    if not text:
        return ""
    text = re.sub(r'[\.\-\=&,\?!:;_\(\)\[\]\{\}\"\']', ' ', text)
    text = re.sub(r'[^\u0590-\u05FFa-zA-Z0-9\s]', '', text)
    return " ".join(text.split())

def perform_wikipedia_search(call_id, query):
    query = re.sub(r'[^\u0590-\u05FFa-zA-Z0-9\s]', ' ', query).strip()
    if not query: return "לא צוין מושג תקין לחיפוש"
    with query_locks[query]:
        if query in search_cache: return search_cache[query]['result']
        try:
            res = session.get("https://he.wikipedia.org/w/api.php", params={"action":"query","list":"search","srsearch":query,"format":"json","srlimit":1}, timeout=10)
            data = res.json().get("query", {}).get("search", [])
            if not data: return "לא נמצא מידע"
            title = data[0]["title"]
            res = session.get("https://he.wikipedia.org/w/api.php", params={"action":"query","prop":"extracts","exintro":True,"explaintext":True,"titles":title,"format":"json"}, timeout=10)
            pages = res.json().get("query", {}).get("pages", {})
            page_id = list(pages.keys())[0]
            extract = pages[page_id].get("extract", "")[:600]
            result = clean_text(f"ויקיפדיה על {title} {extract}")
            search_cache[query] = {'result': result, 'time': time.time()}
            return result
        except Exception as e:
            log_event(call_id, "wikipedia_error", error=str(e))
            return "תקלה בחיפוש בויקיפדיה"

def wikipedia_search(query: str) -> str:
    """Search Wikipedia to get accurate information about terms, people or events."""
    return query

# ============================================================
# נתיבי ניהול — Admin routes
# ============================================================

@app.route('/admin', methods=['GET'])
def admin_page():
    return send_file(ADMIN_HTML_FILE)

@app.route('/admin/send-code', methods=['POST'])
def admin_send_code():
    data = request.get_json(force=True)
    email = (data.get('email') or '').strip().lower()

    if not email or '@' not in email:
        return jsonify({"ok": False, "error": "כתובת מייל לא תקינה"})

    if not TARGET_EMAIL or email != TARGET_EMAIL.strip().lower():
        return jsonify({"ok": False, "error": "כתובת המייל אינה מורשית"})

    if not GOOGLE_SCRIPT_URL:
        return jsonify({"ok": False, "error": "משתנה GOOGLE_SCRIPT_URL לא מוגדר בשרת"})

    code = ''.join(random.choices(string.digits, k=6))
    auth_codes[email] = (code, time.time() + 300)  # תוקף 5 דקות

    try:
        body = f"""
        <div dir="rtl" style="font-family:Arial,sans-serif;font-size:16px;color:#333">
          <p>קוד האימות שלך למערכת נועם:</p>
          <h2 style="letter-spacing:8px;color:#5b6ef5">{code}</h2>
          <p style="color:#888;font-size:13px">הקוד תקף ל-5 דקות בלבד.</p>
        </div>
        """
        session.post(GOOGLE_SCRIPT_URL, json={
            "to": email,
            "subject": "קוד אימות — נועם ניהול",
            "htmlBody": body
        }, timeout=10)
    except Exception as e:
        log_event("admin", "send_code_email_failed", error=str(e))
        return jsonify({"ok": False, "error": "שגיאה בשליחת המייל"})

    log_event("admin", "auth_code_sent", email=email)
    return jsonify({"ok": True})

@app.route('/admin/verify-code', methods=['POST'])
def admin_verify_code():
    data = request.get_json(force=True)
    email = (data.get('email') or '').strip().lower()
    code  = (data.get('code')  or '').strip()

    entry = auth_codes.get(email)
    if not entry:
        return jsonify({"ok": False, "error": "לא נשלח קוד לכתובת זו"})

    saved_code, expires_at = entry
    if time.time() > expires_at:
        del auth_codes[email]
        return jsonify({"ok": False, "error": "הקוד פג תוקף — שלח קוד חדש"})

    if code != saved_code:
        return jsonify({"ok": False, "error": "קוד שגוי"})

    del auth_codes[email]
    token = ''.join(random.choices(string.ascii_letters + string.digits, k=48))
    active_sessions[token] = email
    log_event("admin", "login_success", email=email)
    return jsonify({"ok": True, "token": token})

def require_session():
    token = request.headers.get('X-Token', '')
    return active_sessions.get(token)

@app.route('/admin/numbers', methods=['GET'])
def admin_get_numbers():
    if not require_session():
        return jsonify({"ok": False, "error": "לא מורשה"}), 401
    return jsonify({"ok": True, "numbers": load_allowed_numbers()})

@app.route('/admin/numbers', methods=['POST'])
def admin_save_numbers():
    if not require_session():
        return jsonify({"ok": False, "error": "לא מורשה"}), 401
    data = request.get_json(force=True)
    numbers = [n.strip() for n in (data.get('numbers') or []) if n.strip()]
    save_allowed_numbers(numbers)
    log_event("admin", "numbers_saved", count=len(numbers))
    return jsonify({"ok": True})

# ============================================================
# נתיב ראשי — שיחות טלפון
# ============================================================

@app.route('/ai-chat', methods=['GET', 'POST'])
def ai_chat():
    caller_id = request.values.get('ApiPhone', 'unknown')
    call_id   = request.values.get('ApiCallId', 'unknown_call')

    log_event(call_id, "incoming_call_request", params=dict(request.values))

    if not is_allowed(caller_id):
        log_event(call_id, "unauthorized_caller", caller_id=caller_id)
        return Response("read=t-מצטערים השירות אינו זמין עבורך=hangup", mimetype='text/plain')

    history, known_name = get_chat_data(caller_id)

    if request.values.get('hangup') == 'yes':
        log_event(call_id, "hangup_received")
        delete_chat_data(caller_id)
        return Response("noop", mimetype='text/plain')

    audio_path = request.values.getlist('user_audio')
    if not audio_path:
        log_event(call_id, "first_greeting_prompt")
        return Response(f"read=t-שלום כאן נועם אנא דברו לאחר הצליל={RECORD_COMMAND}", mimetype='text/plain')

    try:
        log_event(call_id, "downloading_audio_file", path=audio_path[-1])
        audio_res = session.get("https://www.call2all.co.il/ym/api/DownloadFile",
                                params={"token": YEMOT_TOKEN, "path": f"ivr2:{audio_path[-1]}"},
                                timeout=20)
        audio_res.raise_for_status()

        content_type = audio_res.headers.get('Content-Type', '').lower()
        if 'text' in content_type or 'html' in content_type or len(audio_res.content) < 1000:
            log_event(call_id, "invalid_audio_file_detected", content_type=content_type, size=len(audio_res.content))
            raise Exception("Downloaded file from Yemot is corrupted, an error page, or too small")

        system_prompt = (
            "You are Noam, a helpful and friendly voice assistant on a phone call. "
            "CRITICAL RULE: Keep your answers VERY SHORT, concise, and conversational. "
            "Respond in 1 to 3 short sentences MAXIMUM per answer. Never give long explanations or lectures. "
            "Speak naturally as if you are talking to a friend on the phone. "
            "INTERNAL KNOWLEDGE FIRST: Always answer directly from your own internal knowledge base first. "
            "ONLY execute a 'wikipedia_search' tool call if you genuinely do not know the answer. "
            "FORMAT RULE: Do NOT use any punctuation marks whatsoever (no periods, no commas, no question marks). "
            "Use only clear Hebrew letters and spaces. Never output English or internal thoughts."
        )

        contents = [types.Content(role='user' if h['role'] == 'user' else 'model',
                                  parts=[types.Part(text=h['content'])]) for h in history]
        contents.append(types.Content(role="user", parts=[
            types.Part.from_bytes(data=audio_res.content, mime_type="audio/wav"),
            types.Part(text="הקשב לקובץ השמע המצורף וענה למשתמש בעברית תשובה קצרה מאוד של עד שלושה משפטים וללא סימני פיסוק כלל.")
        ]))

        gemini_keys = [k for k in [GEMINI_API_KEY, GEMINI_API_KEY_2] if k]
        response_text = None
        user_content_for_history = "[קובץ שמע]"

        for idx, current_key in enumerate(gemini_keys):
            try:
                log_event(call_id, f"trying_gemini_api_key_{idx+1}")
                local_client = genai.Client(api_key=current_key)
                response = local_client.models.generate_content(
                    model=MODEL_NAME,
                    contents=contents,
                    config=types.GenerateContentConfig(system_instruction=system_prompt, tools=[wikipedia_search])
                )

                if response.function_calls:
                    call = response.function_calls[0]
                    log_event(call_id, f"tool_use_triggered_key_{idx+1}", function_name=call.name, args=call.args)
                    res = perform_wikipedia_search(call_id, call.args.get("query", ""))
                    contents.append(response.candidates[0].content)
                    contents.append(types.Content(role="user", parts=[
                        types.Part.from_function_response(name="wikipedia_search", response={"result": res})
                    ]))
                    log_event(call_id, f"sending_tool_response_back_key_{idx+1}")
                    response = local_client.models.generate_content(
                        model=MODEL_NAME,
                        contents=contents,
                        config=types.GenerateContentConfig(system_instruction=system_prompt)
                    )

                temp_text = response.text or ""
                if "מצטער" in temp_text and "להקשיב" in temp_text:
                    raise Exception("Gemini hallucinated a file-reading refusal text")

                response_text = response.text
                log_event(call_id, f"gemini_key_{idx+1}_success")
                break
            except Exception as gemini_err:
                log_event(call_id, f"gemini_key_{idx+1}_failed", error=str(gemini_err))
                continue

        if not response_text:
            if GROQ_API_KEY:
                try:
                    log_event(call_id, "groq_fallback_triggered")
                    whisper_url = "https://api.groq.com/openai/v1/audio/transcriptions"
                    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
                    files = {"file": ("audio.wav", audio_res.content, "audio/wav")}
                    data = {"model": GROQ_WHISPER_MODEL, "language": "he"}
                    whisper_res = session.post(whisper_url, headers=headers, files=files, data=data, timeout=15)
                    whisper_res.raise_for_status()
                    user_transcription = whisper_res.json().get("text", "")
                    log_event(call_id, "groq_whisper_success", text=user_transcription)
                    user_content_for_history = f"🎙️ {user_transcription}"

                    chat_url = "https://api.groq.com/openai/v1/chat/completions"
                    messages = [{"role": "system", "content": system_prompt}]
                    for h in history:
                        messages.append({"role": h['role'], "content": h['content']})
                    messages.append({"role": "user", "content": user_transcription})
                    payload = {"model": GROQ_CHAT_MODEL, "messages": messages}
                    chat_res = session.post(chat_url,
                                            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
                                            json=payload, timeout=15)
                    if chat_res.status_code != 200:
                        log_event(call_id, "groq_chat_failed_body", status=chat_res.status_code, body=chat_res.text)
                    chat_res.raise_for_status()
                    response_text = chat_res.json()['choices'][0]['message']['content']
                    log_event(call_id, "groq_chat_success")
                except Exception as groq_err:
                    log_event(call_id, "groq_fallback_failed", error=str(groq_err))
                    raise Exception("All API keys and Groq fallback exhausted")
            else:
                raise Exception("Gemini failed and GROQ_API_KEY is missing")

        ai_reply = clean_text(response_text)
        log_event(call_id, "final_response_success", reply=ai_reply)
        history.extend([{"role": "user", "content": user_content_for_history},
                         {"role": "assistant", "content": ai_reply}])
        save_chat_data(caller_id, history, known_name)
        return Response(f"read=t-{ai_reply}={RECORD_COMMAND}", mimetype='text/plain')

    except Exception as e:
        error_trace = traceback.format_exc()
        print(f"--- CRITICAL ERROR FOR CALL {call_id} ---", file=sys.stderr)
        print(error_trace, file=sys.stderr, flush=True)
        log_event(call_id, "global_exception_caught", error=str(e))
        err_msg_lower = str(e).lower()
        if "exhausted" in err_msg_lower or "429" in err_msg_lower or "limit" in err_msg_lower:
            friendly_message = "חלקה שגיאה זמנית במערכת עקב עומס בקשות רב מדי אנא נסו להתקשר שוב בעוד מספר דקות"
        elif "api key" in err_msg_lower or "401" in err_msg_lower or "unauthorized" in err_msg_lower:
            friendly_message = "חלקה שגיאת אימות במפתחות הגישה של השרת אנא פנו למנהל המערכת לעדכון המפתחות"
        elif "timeout" in err_msg_lower or "connection" in err_msg_lower:
            friendly_message = "החיבור לשרתי השירות נתקע עקב בעיית תקשורת זמנית אנא נסו שוב"
        else:
            friendly_message = "חלקה שגיאה טכנית זמנית בעיבוד הנתונים של השיחה אנא נסו שוב מאוחר יותר"
        friendly_message = clean_text(friendly_message)
        return Response(f"read=t-{friendly_message}", mimetype='text/plain')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)

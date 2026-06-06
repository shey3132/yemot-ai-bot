import os
import time
import re
import json
import sqlite3
import html
import logging
import atexit
from io import BytesIO
from collections import defaultdict
from contextlib import closing
from threading import Lock
from concurrent.futures import ThreadPoolExecutor

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from flask import Flask, request, Response
from google import genai
from google.genai import types

app = Flask(__name__)

# --- הגדרות סביבה ---
YEMOT_TOKEN = os.environ.get("YEMOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GOOGLE_SCRIPT_URL = os.environ.get("GOOGLE_SCRIPT_URL")
TARGET_EMAIL = os.environ.get("TARGET_EMAIL")

logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger(__name__)

def log_event(call_id, event_name, **kwargs):
    log_data = {"call_id": call_id, "event": event_name, "timestamp": time.time()}
    log_data.update(kwargs)
    logger.info(json.dumps(log_data))

session = requests.Session()
retry_strategy = Retry(total=3, backoff_factor=0.3, status_forcelist=[429, 500, 502, 503, 504])
adapter = HTTPAdapter(max_retries=retry_strategy)
session.mount("http://", adapter)
session.mount("https://", adapter)

# התיקון הקריטי כאן: הגדרת api_version ל-v1 כדי למנוע את שגיאת ה-404
client = genai.Client(api_key=GEMINI_API_KEY, http_options={'api_version': 'v1'})
RECORD_COMMAND = "user_audio,no,record,,,yes,yes,no,1,120"
DB_FILE = "chat_memory.db"
search_cache = {}
global_cache_lock = Lock()
query_locks = defaultdict(Lock)
email_executor = ThreadPoolExecutor(max_workers=10)
atexit.register(lambda: email_executor.shutdown(wait=False))

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
        except: return "תקלה בחיפוש"

def generate_smart_summary(call_id, history):
    try:
        text_log = "\n".join([f"{msg['role']}: {msg.get('content', '')}" for msg in history if 'tool_calls' not in msg])
        response = client.models.generate_content(
            model='gemini-1.5-flash',
            contents=f"סכם את השיחה: {text_log}"
        )
        return response.text.strip()
    except: return "לא ניתן להפיק תקציר"

def send_summary_email(call_id, caller_id, history_copy, name):
    if not GOOGLE_SCRIPT_URL or not TARGET_EMAIL: return
    try:
        raw_summary = generate_smart_summary(call_id, history_copy)
        session.post(GOOGLE_SCRIPT_URL, json={"to": TARGET_EMAIL, "subject": f"סיכום שיחה {name}", "htmlBody": raw_summary}, timeout=10)
    except: pass

def wikipedia_search(query: str) -> str:
    """Search Wikipedia"""
    return query

@app.route('/ai-chat', methods=['GET', 'POST'])
def ai_chat():
    caller_id = request.values.get('ApiPhone', 'unknown')
    call_id = request.values.get('ApiCallId', 'unknown_call')
    history, known_name = get_chat_data(caller_id)

    if request.values.get('hangup') == 'yes':
        if history: email_executor.submit(send_summary_email, call_id, caller_id, history.copy(), known_name)
        delete_chat_data(caller_id)
        return Response("noop", mimetype='text/plain')

    audio_path = request.values.getlist('user_audio')
    if not audio_path:
        return Response(f"read=t-שלום כאן נועם אנא דברו לאחר הצליל={RECORD_COMMAND}", mimetype='text/plain')

    audio_res = session.get("https://www.call2all.co.il/ym/api/DownloadFile", params={"token": YEMOT_TOKEN, "path": f"ivr2:{audio_path[-1]}"}, timeout=20)
    
    system_prompt = "You are Noam, an AI on a phone call. Answer warmly in Hebrew. Do NOT use punctuation. End with a question."
    
    contents = [types.Content(role='user' if h['role'] == 'user' else 'model', parts=[types.Part.from_text(h['content'])]) for h in history]
    contents.append(types.Content(role="user", parts=[
        types.Part.from_bytes(data=audio_res.content, mime_type="audio/wav"),
        types.Part.from_text(text="הקשב לאודיו וענה. ללא סימני פיסוק.")
    ]))

    try:
        response = client.models.generate_content(
            model='gemini-1.5-flash',
            contents=contents,
            config=types.GenerateContentConfig(system_instruction=system_prompt, tools=[wikipedia_search])
        )
        
        if response.function_calls:
            call = response.function_calls[0]
            res = perform_wikipedia_search(call_id, call.args.get("query", ""))
            contents.append(response.candidates[0].content)
            contents.append(types.Content(role="user", parts=[types.Part.from_function_response(name="wikipedia_search", response={"result": res})]))
            response = client.models.generate_content(model='gemini-1.5-flash', contents=contents)

        ai_reply = clean_text(response.text)
        history.extend([{"role": "user", "content": "[קובץ שמע]"}, {"role": "assistant", "content": ai_reply}])
        save_chat_data(caller_id, history, known_name)
        return Response(f"read=t-{ai_reply}={RECORD_COMMAND}", mimetype='text/plain')
    except Exception as e:
        log_event(call_id, "global_error", error=str(e))
        return Response(f"read=t-תקלה זמנית אנא נסו שוב={RECORD_COMMAND}", mimetype='text/plain')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)

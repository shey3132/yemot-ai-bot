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

# --- מערכת לוגים ---
logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger(__name__)

def log_event(call_id, event_name, **kwargs):
    log_data = {"call_id": call_id, "event": event_name, "timestamp": time.time()}
    log_data.update(kwargs)
    logger.info(json.dumps(log_data))

# --- Session & Retry ---
session = requests.Session()
retry_strategy = Retry(total=3, backoff_factor=0.3, status_forcelist=[429, 500, 502, 503, 504])
adapter = HTTPAdapter(max_retries=retry_strategy)
session.mount("http://", adapter)
session.mount("https://", adapter)

# --- Gemini Client ---
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
    if not query:
        return "לא צוין מושג תקין לחיפוש"

    now = time.time()
    with global_cache_lock:
        keys_to_delete = [k for k, v in search_cache.items() if now - v['time'] > 300]
        for k in keys_to_delete:
            del search_cache[k]

    with query_locks[query]:
        if query in search_cache:
            return search_cache[query]['result']

        try:
            search_url = "https://he.wikipedia.org/w/api.php"
            search_params = {
                "action": "query",
                "list": "search",
                "srsearch": query,
                "format": "json",
                "srlimit": 2
            }
            search_res = session.get(search_url, params=search_params, timeout=12)
            search_res.raise_for_status()
            search_data = search_res.json()
            search_results = search_data.get("query", {}).get("search", [])
            if not search_results:
                return "לא נמצא מידע מדויק באנציקלופדיה"

            page_title = search_results[0]["title"]
            content_params = {
                "action": "query",
                "prop": "extracts",
                "exintro": True,
                "explaintext": True,
                "titles": page_title,
                "format": "json"
            }
            content_res = session.get(search_url, params=content_params, timeout=10)
            content_res.raise_for_status()
            content_data = content_res.json()
            pages = content_data.get("query", {}).get("pages", {})
            page_id = list(pages.keys())[0]
            if page_id == "-1":
                return "לא נמצא תוכן מפורט"
            extract = pages[page_id].get("extract", "").strip()
            short_extract = extract[:650]
            final_result = f"מתוך ויקיפדיה על {page_title} {short_extract}"
            final_result = clean_text(final_result)
            with global_cache_lock:
                search_cache[query] = {'result': final_result, 'time': time.time()}
            return final_result
        except Exception as e:
            log_event(call_id, "wikipedia_search_error", error=str(e), query=query)
            return "לא ניתן לשלוף מידע ברגע זה"

def generate_smart_summary(call_id, history):
    try:
        text_log = "\n".join([f"{msg['role']}: {msg.get('content', '')}" for msg in history if 'tool_calls' not in msg])
        prompt = "סכם את השיחה ב2-3 נקודות מרכזיות בלבד"

        response = client.models.generate_content(
            model="gemini-1.5-flash",
            contents=f"{prompt}\n\n{text_log}"
        )
        return response.text.strip()
    except Exception as e:
        log_event(call_id, "summary_generation_error", error=str(e))
        return "לא ניתן להפיק תקציר."

# --- ממשק בריאות ---
@app.route('/health')
def health_check():
    return "ok", 200

@app.route('/ready')
def ready_check():
    return "ready", 200

# --- נקודת כניסה עיקרית ---
@app.route('/ai-chat', methods=['GET', 'POST'])
def ai_chat():
    req_t0 = time.perf_counter()

    if 'ApiPhone' not in request.values and 'ApiCallId' not in request.values:
        return Response("Unauthorized Request", status=401, mimetype='text/plain')

    caller_id = request.values.get('ApiPhone', 'unknown')
    call_id = request.values.get('ApiCallId', 'unknown_call')

    log_event(call_id, "incoming_request")
    history, known_name = get_chat_data(caller_id)

    if request.values.get('hangup') == 'yes':
        if history:
            email_executor.submit(lambda: generate_smart_summary(call_id, history))
        delete_chat_data(caller_id)
        log_event(call_id, "call_ended")
        return Response("noop", status=200, mimetype='text/plain')

    return Response(f"read=t-שלום כאן נועם העוזר הקולי שלכם אנא דברו לאחר הצליל={RECORD_COMMAND}", status=200, mimetype='text/plain')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)

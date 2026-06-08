import os
import time
import re
import json
import sqlite3
import html
import sys
import traceback
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

# מודל הדגל המעודכן של ה-SDK החדש לביצועים מקסימליים ומניעת 404
MODEL_NAME = "gemini-2.5-flash"

# פונקציית לוגים אמינה שמדפיסה מיד למסוף של Render
def log_event(call_id, event_name, **kwargs):
    log_data = {"call_id": call_id, "event": event_name, "timestamp": time.time()}
    log_data.update(kwargs)
    print(json.dumps(log_data), flush=True)

# אתחול הלקוח של גוגל
client = genai.Client(api_key=GEMINI_API_KEY)

session = requests.Session()
retry_strategy = Retry(total=3, backoff_factor=0.3, status_forcelist=[429, 500, 502, 503, 504])
adapter = HTTPAdapter(max_retries=retry_strategy)
session.mount("http://", adapter)
session.mount("https://", adapter)

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
    # הסרת כל סימני הבקרה והפיסוק האסורים בימות המשיח
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

def generate_smart_summary(call_id, history):
    try:
        text_log = "\n".join([f"{msg['role']}: {msg.get('content', '')}" for msg in history if 'tool_calls' not in msg])
        response = client.models.generate_content(
            model=MODEL_NAME,
            contents=f"סכם את השיחה הטלפונית הבאה בנקודות קצרות וברורות:\n{text_log}"
        )
        return response.text.strip()
    except Exception as e:
        log_event(call_id, "summary_generation_failed", error=str(e))
        return "לא ניתן להפיק תקציר עבור שיחה זו"

def send_summary_email(call_id, caller_id, history_copy, name):
    if not GOOGLE_SCRIPT_URL or not TARGET_EMAIL: return
    try:
        raw_summary = generate_smart_summary(call_id, history_copy)
        session.post(GOOGLE_SCRIPT_URL, json={"to": TARGET_EMAIL, "subject": f"סיכום שיחה מפורט - {name or caller_id}", "htmlBody": raw_summary}, timeout=10)
        log_event(call_id, "email_sent_successfully")
    except Exception as e:
        log_event(call_id, "email_sending_failed", error=str(e))

def wikipedia_search(query: str) -> str:
    """Search Wikipedia to get accurate information about terms, people or events."""
    return query

@app.route('/ai-chat', methods=['GET', 'POST'])
def ai_chat():
    caller_id = request.values.get('ApiPhone', 'unknown')
    call_id = request.values.get('ApiCallId', 'unknown_call')
    
    log_event(call_id, "incoming_call_request", params=dict(request.values))
    history, known_name = get_chat_data(caller_id)

    if request.values.get('hangup') == 'yes':
        log_event(call_id, "hangup_received")
        if history: 
            email_executor.submit(send_summary_email, call_id, caller_id, history.copy(), known_name)
        delete_chat_data(caller_id)
        return Response("noop", mimetype='text/plain')

    audio_path = request.values.getlist('user_audio')
    if not audio_path:
        log_event(call_id, "first_greeting_prompt")
        return Response(f"read=t-שלום כאן נועם העוזר הקולי שלכם אנא דברו לאחר הצליל={RECORD_COMMAND}", mimetype='text/plain')

    try:
        log_event(call_id, "downloading_audio_file", path=audio_path[-1])
        audio_res = session.get("https://www.call2all.co.il/ym/api/DownloadFile", params={"token": YEMOT_TOKEN, "path": f"ivr2:{audio_path[-1]}"}, timeout=20)
        audio_res.raise_for_status()
        
        system_prompt = (
            "You are Noam, a helpful voice assistant on a phone call. "
            "Respond warmly and concisely in Hebrew. "
            "CRITICAL FORMAT RULE: Do NOT use any punctuation marks whatsoever in your text output. No periods, no commas, no hyphens, no question marks. "
            "Use only clear Hebrew letters and spaces. End your speech response with a brief natural question."
        )
        
        contents = [types.Content(role='user' if h['role'] == 'user' else 'model', parts=[types.Part.from_text(h['content'])]) for h in history]
        contents.append(types.Content(role="user", parts=[
            types.Part.from_bytes(data=audio_res.content, mime_type="audio/wav"),
            types.Part.from_text(text="הקשב לקובץ השמע המצורף וענה למשתמש בעברית ללא סימני פיסוק כלל.")
        ]))

        log_event(call_id, "sending_to_gemini_api")
        response = client.models.generate_content(
            model=MODEL_NAME,
            contents=contents,
            config=types.GenerateContentConfig(system_instruction=system_prompt, tools=[wikipedia_search])
        )
        
        if response.function_calls:
            call = response.function_calls[0]
            log_event(call_id, "tool_use_triggered", function_name=call.name, args=call.args)
            res = perform_wikipedia_search(call_id, call.args.get("query", ""))
            
            contents.append(response.candidates[0].content)
            contents.append(types.Content(role="user", parts=[
                types.Part.from_function_response(name="wikipedia_search", response={"result": res})
            ]))
            
            log_event(call_id, "sending_tool_response_back_to_gemini")
            response = client.models.generate_content(
                model=MODEL_NAME, 
                contents=contents,
                config=types.GenerateContentConfig(system_instruction=system_prompt)
            )

        ai_reply = clean_text(response.text)
        log_event(call_id, "gemini_response_success", reply=ai_reply)
        
        history.extend([{"role": "user", "content": "[קובץ שמע]"}, {"role": "assistant", "content": ai_reply}])
        save_chat_data(caller_id, history, known_name)
        
        return Response(f"read=t-{ai_reply}={RECORD_COMMAND}", mimetype='text/plain')
        
    except Exception as e:
        # הדפסת ה-Traceback המלא ישירות למסוף כדי שתראה בדיוק מה נכשל
        error_trace = traceback.format_exc()
        print(f"--- CRITICAL ERROR FOR CALL {call_id} ---", file=sys.stderr)
        print(error_trace, file=sys.stderr, flush=True)
        
        log_event(call_id, "global_exception_caught", error=str(e))
        return Response(f"read=t-אנא חזרו על הדברים שנית לאחר הצליל={RECORD_COMMAND}", mimetype='text/plain')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)

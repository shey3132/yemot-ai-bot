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

# --- מערכת לוגים מובנים ---
logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger(__name__)

def log_event(call_id, event_name, **kwargs):
    log_data = {"call_id": call_id, "event": event_name, "timestamp": time.time()}
    log_data.update(kwargs)
    logger.info(json.dumps(log_data))

# --- סשן ולקוחות רשת ---
session = requests.Session()
retry_strategy = Retry(total=3, backoff_factor=0.3, status_forcelist=[429, 500, 502, 503, 504])
adapter = HTTPAdapter(max_retries=retry_strategy)
session.mount("http://", adapter)
session.mount("https://", adapter)

# אתחול הלקוח של גוגל
# במקום מה שהיה, תשתמש בזה:
client = genai.Client(api_key=GEMINI_API_KEY, http_options={'api_version': 'v1'})

# פקודת ההקלטה לימות המשיח - 10 פרמטרים מופרדים בפסיק
# ParameterName,no,record,[Path],[FileName],[NoMenu],[SaveHangup],Append,[MinLen],[MaxLen]
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
    """
    ניקוי קפדני לימות המשיח: מסיר נקודות, מקפים, שווה, אמפרסנד וכל סימן פיסוק אחר.
    משאיר רק אותיות עבריות/אנגליות, מספרים ורווחים.
    """
    if not text:
        return ""
    # הסרת כל סימני הפיסוק והבקרה באופן גורף
    text = re.sub(r'[\.\-\=&,\?!:;_\(\)\[\]\{\}\"\']', ' ', text)
    # ניקוי סופי ליתר ביטחון
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
        
        t0 = time.perf_counter()
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
            
            log_event(call_id, "wikipedia_search_success", duration_sec=round(time.perf_counter() - t0, 3), query=query)
            with global_cache_lock:
                search_cache[query] = {'result': final_result, 'time': time.time()}
            return final_result
            
        except Exception as e:
            log_event(call_id, "wikipedia_search_error", error=str(e), query=query)
            return "לא ניתן לשלוף מידע ברגע זה"

def generate_smart_summary(call_id, history):
    try:
        text_log = "\n".join([f"{msg['role']}: {msg.get('content', '')}" for msg in history if 'tool_calls' not in msg])
        prompt = "סכם את השיחה הבאה ב-2 עד 3 נקודות קצרות: נושא מרכזי, בקשת המשתמש ומה סוכם. החזר טקסט בלבד ללא HTML."
        
        response = client.models.generate_content(
            model='',
            contents=f"{prompt}\n\n{text_log}"
        )
        return response.text.strip()
    except Exception as e:
        log_event(call_id, "summary_generation_error", error=str(e))
        return "לא ניתן להפיק תקציר."

def send_summary_email(call_id, caller_id, history_copy, name):
    if not GOOGLE_SCRIPT_URL or not TARGET_EMAIL or not history_copy:
        return
    try:
        raw_summary = generate_smart_summary(call_id, history_copy) if len(history_copy) > 5 else "שיחה קצרה מדי."
        safe_summary = html.escape(raw_summary)
        safe_name = html.escape((name or "משתמש לא ידוע")[:100])
        safe_caller_id = html.escape((caller_id or "לא חסוי")[:50])
        subject = f"סיכום שיחה מלא - נועם AI: {safe_name} ({safe_caller_id})"
        
        body = f"""
        <div style="font-family: 'Segoe UI', sans-serif; direction: rtl; max-width: 650px; margin: 20px auto; border: 1px solid #eaeaea; border-radius: 8px; box-shadow: 0 4px 6px rgba(0,0,0,0.05); overflow: hidden; background-color: #ffffff;">
            <div style="background-color: #0f172a; color: #ffffff; padding: 25px; text-align: center; border-bottom: 4px solid #3b82f6;">
                <h2 style="margin: 0; font-size: 24px;">סיכום שיחה נכנסת - נועם AI</h2>
            </div>
            <div style="padding: 20px; background-color: #f8fafc; border-bottom: 1px solid #e2e8f0; font-size: 15px; color: #334155;">
                <p><strong>תקציר השיחה:</strong></p>
                <div>{safe_summary}</div>
            </div>
            <div style="padding: 25px; font-size: 15px; line-height: 1.6;">
        """
        for msg in history_copy:
            if msg.get("content"):
                role_display = "משתמש" if msg["role"] == "user" else "נועם"
                color_bg = "#e0f2fe" if msg["role"] == "user" else "#f1f5f9"
                color_border = "#0ea5e9" if msg["role"] == "user" else "#64748b"
                content = html.escape(msg.get("content", ""))
                body += f'<div style="margin-bottom: 15px; padding: 12px 15px; background-color: {color_bg}; border-right: 4px solid {color_border}; border-radius: 4px;"><strong>{role_display}:</strong><br>{content}</div>'
        
        body += "</div></div>"
        session.post(GOOGLE_SCRIPT_URL, json={"to": TARGET_EMAIL, "subject": subject, "htmlBody": body}, timeout=15)
        log_event(call_id, "email_sent_success")
    except Exception as e:
        log_event(call_id, "email_send_error", error=str(e))

def wikipedia_search(query: str) -> str:
    """USE ONLY WHEN UNSURE. Search Wikipedia to verify facts, definitions, or historical data."""
    return query

@app.route('/health')
def health_check():
    return "ok", 200

@app.route('/ready')
def ready_check():
    return "ready", 200

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
            email_executor.submit(send_summary_email, call_id, caller_id, history.copy(), known_name)
        delete_chat_data(caller_id)
        log_event(call_id, "call_ended")
        return Response("noop", status=200, mimetype='text/plain')

    audio_list = request.values.getlist('user_audio')
    audio_path = audio_list[-1] if audio_list else None

    if not audio_path:
        log_event(call_id, "prompt_user")
        return Response(f"read=t-שלום כאן נועם העוזר הקולי שלכם אנא דברו לאחר הצליל={RECORD_COMMAND}", status=200, mimetype='text/plain')

    yemot_path = f"ivr2:{audio_path}"

    try:
        dl_t0 = time.perf_counter()
        audio_res = session.get("https://www.call2all.co.il/ym/api/DownloadFile", params={"token": YEMOT_TOKEN, "path": yemot_path}, timeout=20)
        audio_res.raise_for_status() 
        log_event(call_id, "audio_downloaded", duration_sec=round(time.perf_counter() - dl_t0, 3))
        
        audio_bytes = audio_res.content
        
        system_prompt = (
            "You are Noam, a smart AI assistant on a phone call. "
            "KNOWLEDGE RULE: ALWAYS rely on your vast internal knowledge first. ONLY call the wikipedia_search tool if you are completely unsure. "
            "RULES FOR SPEECH RESPONSE: Speak warmly and naturally in Hebrew. Never make up facts. "
            "CRITICAL: Do NOT use periods (.), hyphens (-), equals (=), ampersands (&), question marks, commas, or any other punctuation. Use letters and spaces only. "
            "Always end your final spoken response with a short question to keep the conversation going."
        )

        contents = []
        for h in history:
            role = 'user' if h['role'] == 'user' else 'model'
            contents.append(types.Content(role=role, parts=[types.Part.from_text(text=h['content'])]))
            
        contents.append(
            types.Content(
                role="user",
                parts=[
                    types.Part.from_bytes(data=audio_bytes, mime_type="audio/wav"),
                    types.Part.from_text(text="הקשב לקובץ האודיו המצורף וענה למשתמש בהתאם להנחיות הסיסטם. זכור: ללא סימני פיסוק כלל.")
                ]
            )
        )

        config = types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=0.1,
            max_output_tokens=150,
            tools=[wikipedia_search],
        )

        llm_t0 = time.perf_counter()
        response = client.models.generate_content(
            model='',
            contents=contents,
            config=config
        )
        log_event(call_id, "gemini_llm_pass", duration_sec=round(time.perf_counter() - llm_t0, 3))

        if response.function_calls:
            for function_call in response.function_calls:
                if function_call.name == "wikipedia_search":
                    args = function_call.args
                    search_query = args.get("query", "")
                    search_result = perform_wikipedia_search(call_id, search_query)
                    
                    contents.append(response.candidates[0].content)
                    contents.append(
                        types.Content(
                            role="user",
                            parts=[
                                types.Part.from_function_response(
                                    name="wikipedia_search",
                                    response={"result": search_result}
                                )
                            ]
                        )
                    )
                    
            llm_t1 = time.perf_counter()
            response = client.models.generate_content(
                model='',
                contents=contents,
                config=config
            )
            log_event(call_id, "gemini_llm_pass_2", duration_sec=round(time.perf_counter() - llm_t1, 3))

        ai_reply = response.text.strip()
        
        history.append({"role": "user", "content": "[קובץ שמע שפורש על ידי המערכת]"})
        history.append({"role": "assistant", "content": ai_reply})

        save_chat_data(caller_id, history, known_name)
        log_event(call_id, "request_completed", total_duration_sec=round(time.perf_counter() - req_t0, 3))
        
        safe_response_text = clean_text(ai_reply)
        return Response(f"read=t-{safe_response_text}={RECORD_COMMAND}", status=200, mimetype='text/plain')

    except Exception as e:
        log_event(call_id, "global_error", error=str(e))
        return Response(f"read=t-סליחה תקלה זמנית בעיבוד הנתונים אנא נסו שוב={RECORD_COMMAND}", status=200, mimetype='text/plain')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)

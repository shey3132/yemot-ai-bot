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
from flask import Flask, request, jsonify
from groq import Groq

app = Flask(__name__)

# --- הגדרות סביבה ---
YEMOT_TOKEN = os.environ.get("YEMOT_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GOOGLE_SCRIPT_URL = os.environ.get("GOOGLE_SCRIPT_URL")
TARGET_EMAIL = os.environ.get("TARGET_EMAIL")
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY") 
GOOGLE_CX = os.environ.get("GOOGLE_CX") 

# --- מערכת לוגים מובנים (Structured Logging) ---
logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger(__name__)

def log_event(call_id, event_name, **kwargs):
    """ הפקת לוגים בפורמט JSON לטובת כלי ניטור (Datadog/Grafana) """
    log_data = {"call_id": call_id, "event": event_name, "timestamp": time.time()}
    log_data.update(kwargs)
    logger.info(json.dumps(log_data))

# --- סשן ולקוחות רשת ---
session = requests.Session()
retry_strategy = Retry(total=3, backoff_factor=0.3, status_forcelist=[429, 500, 502, 503, 504])
adapter = HTTPAdapter(max_retries=retry_strategy)
session.mount("http://", adapter)
session.mount("https://", adapter)

client = Groq(api_key=GROQ_API_KEY, timeout=20.0)

# --- תצורות ---
RECORD_COMMAND = "user_audio,no,record,,,yes,yes,no,1,120"
DB_FILE = "chat_memory.db"

# מטמון מוגן לחיפושי גוגל (כולל פתרון ל-Cache Stampede)
google_cache = {}
global_cache_lock = Lock()
query_locks = defaultdict(Lock) # נעילה ברמת מונח חיפוש

# Pool לשליחת מיילים וכיבוי מסודר
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
        history_to_save = history[-30:]
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
    # שומר על תווים רלוונטיים (מיילים, קישורים, אחוזים) ומנקה רעש
    text = re.sub(r'[^\u0590-\u05FFa-zA-Z0-9\s@\.\-\+:/%]', '', text)
    return " ".join(text.split())


def get_safe_history(history, target_len=12):
    if len(history) <= target_len:
        return history
    
    sliced = history[-target_len:]
    while sliced and sliced[0]["role"] in ["tool", "assistant"]:
        if sliced[0]["role"] == "assistant" and "tool_calls" not in sliced[0]:
            break
        sliced.pop(0)
        
    return sliced if sliced else history[-2:]


def perform_google_search(call_id, query):
    if not GOOGLE_API_KEY or not GOOGLE_CX:
        return "מנגנון החיפוש לא הוגדר במערכת"
    
    now = time.time()
    
    # 1. מניעת זליגת זיכרון מחייבת נעילה גלובלית קצרה
    with global_cache_lock:
        keys_to_delete = [k for k, v in google_cache.items() if now - v['time'] > 300]
        for k in keys_to_delete:
            del google_cache[k]
            
    # 2. מניעת Cache Stampede: נעילה ספציפית למונח החיפוש
    with query_locks[query]:
        # בדיקה אם החיפוש כבר קיים במטמון
        if query in google_cache:
            return google_cache[query]['result']
        
        # אם אין, ה־Thread הנוכחי יבצע את החיפוש
        t0 = time.perf_counter()
        url = f"https://www.googleapis.com/customsearch/v1?q={query}&key={GOOGLE_API_KEY}&cx={GOOGLE_CX}"
        try:
            res = session.get(url, timeout=10)
            res.raise_for_status()
            data = res.json()
            items = data.get("items", [])
            
            final_result = "לא נמצאו תוצאות לחיפוש זה"
            if items:
                results = [f"{item['title']} - {item['snippet']}" for item in items[:2]]
                final_result = "תוצאות מהרשת: " + " ".join(results)
                
            log_event(call_id, "google_search_success", duration_sec=round(time.perf_counter() - t0, 3), query=query)
            
            # עדכון המטמון (נעילה גלובלית קצרה)
            with global_cache_lock:
                google_cache[query] = {'result': final_result, 'time': time.time()}
                
            return final_result
            
        except Exception as e:
            log_event(call_id, "google_search_error", error=str(e), query=query)
            return "הייתה שגיאה בחיפוש ברשת"


def generate_smart_summary(call_id, history):
    t0 = time.perf_counter()
    try:
        text_log = "\n".join([f"{msg['role']}: {msg.get('content', '')}" for msg in history if 'tool_calls' not in msg])
        prompt = "סכם את השיחה הבאה ב-2 עד 3 נקודות קצרות: נושא מרכזי, בקשת המשתמש ומה סוכם. החזר טקסט בלבד ללא HTML."
        response = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{"role": "system", "content": prompt}, {"role": "user", "content": text_log}],
            temperature=0.3, max_tokens=200
        )
        log_event(call_id, "summary_generation_success", duration_sec=round(time.perf_counter() - t0, 3))
        return response.choices[0].message.content.strip()
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
        subject = f"📄 סיכום שיחה מלא - נועם AI: {safe_name} ({safe_caller_id})"
        
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
            role_display = "משתמש" if msg["role"] == "user" else "מערכת" if msg["role"] == "tool" else "נועם"
            color_bg = "#e0f2fe" if msg["role"] == "user" else "#fef3c7" if msg["role"] == "tool" else "#f1f5f9"
            color_border = "#0ea5e9" if msg["role"] == "user" else "#f59e0b" if msg["role"] == "tool" else "#64748b"
            
            if "tool_calls" not in msg:
                content = html.escape(msg.get("content", ""))
                body += f'<div style="margin-bottom: 15px; padding: 12px 15px; background-color: {color_bg}; border-right: 4px solid {color_border}; border-radius: 4px;"><strong>{role_display}:</strong><br>{content}</div>'
        
        body += "</div></div>"
        
        session.post(GOOGLE_SCRIPT_URL, json={"to": TARGET_EMAIL, "subject": subject, "htmlBody": body}, timeout=15)
        log_event(call_id, "email_sent_success")
        
    except Exception as e:
        log_event(call_id, "email_send_error", error=str(e))


@app.route('/health')
def health_check():
    return jsonify({"status": "ok", "timestamp": time.time()}), 200

@app.route('/ready')
def ready_check():
    """ בדיקת Readiness מקיפה - DB ומשתני סביבה """
    checks = {
        "db": False,
        "groq_key": bool(GROQ_API_KEY),
        "google_key": bool(GOOGLE_API_KEY and GOOGLE_CX)
    }
    try:
        with closing(sqlite3.connect(DB_FILE, timeout=5)) as conn:
            conn.execute("SELECT 1").fetchone()
        checks["db"] = True
    except Exception:
        pass
        
    status_code = 200 if all(checks.values()) else 503
    return jsonify({"status": "ready" if status_code == 200 else "error", "checks": checks}), status_code


@app.route('/ai-chat', methods=['GET', 'POST'])
def ai_chat():
    req_t0 = time.perf_counter()
    
    if 'ApiPhone' not in request.values and 'ApiCallId' not in request.values:
        return "Unauthorized Request", 401

    caller_id = request.values.get('ApiPhone', 'unknown')
    call_id = request.values.get('ApiCallId', 'unknown_call')
    
    log_event(call_id, "incoming_request")
    
    history, known_name = get_chat_data(caller_id)

    if request.values.get('hangup') == 'yes':
        if history:
            email_executor.submit(send_summary_email, call_id, caller_id, history.copy(), known_name)
        delete_chat_data(caller_id)
        log_event(call_id, "call_ended")
        return "noop", 200

    audio_list = request.values.getlist('user_audio')
    audio_path = audio_list[-1] if audio_list else None

    if not audio_path:
        log_event(call_id, "prompt_user")
        return f"read=t-שלום כאן נועם העוזר הקולי שלך אנא דברו לאחר הצליל ובסיום הקישו סולמית={RECORD_COMMAND}", 200

    yemot_path = f"ivr2:{audio_path}"

    try:
        # הורדת אודיו לזיכרון בלבד (ללא שמירת קובץ לדיסק)
        dl_t0 = time.perf_counter()
        audio_res = session.get("https://www.call2all.co.il/ym/api/DownloadFile", params={"token": YEMOT_TOKEN, "path": yemot_path}, timeout=20)
        audio_res.raise_for_status() 
        log_event(call_id, "audio_downloaded", duration_sec=round(time.perf_counter() - dl_t0, 3))
        
        audio_buffer = BytesIO(audio_res.content)
        audio_buffer.name = "audio.wav"

        # מדידת זמן תמלול
        whisp_t0 = time.perf_counter()
        transcript = client.audio.transcriptions.create(
            file=("audio.wav", audio_buffer.read()),
            model="whisper-large-v3-turbo",
            language="he",
            prompt="היי, זו שיחה טלפונית בעברית. מילים נפוצות: נועם, עוזר קולי.",
            temperature=0.0
        )
        user_text = transcript.text.strip()
        log_event(call_id, "whisper_transcription", duration_sec=round(time.perf_counter() - whisp_t0, 3), input_length=len(user_text))

        history.append({"role": "user", "content": user_text})

        system_prompt = (
            "אתה נועם עוזר קולי של הארגון מדבר בטלפון עם המשתמש "
            "אתה מדבר בגובה העיניים בשפה פשוטה זורמת ויומיומית "
            "אל תאשר קבלה של כל משפט אל תגיד אוקיי או הבנתי פשוט תענה ישר ולעניין "
            "איסור מוחלט על סימני פיסוק אל תשתמש בנקודה פסיק מקף שווה אמפרסנד או סימן שאלה בתשובה שלך "
            "אם יש צורך במספרים קרא אותם ללא סמלים "
            " אם אתה משתמש בחיפוש גוגל חובה לזקק מהמשתמש רק מילות מפתח ממוקדות ואך ורק אם זה על מידע שאתה לא יודע"
           "תמיד תסיים בשאלה קצרה שמניעה להמשך שיחה "
             "אל תקצר במילים כשצריך "
        )

        tools = [{"type": "function", "function": {"name": "google_search", "description": "ביצוע חיפוש באינטרנט במנוע של גוגל.", "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "מילות מפתח בלבד לחיפוש בגוגל."}}, "required": ["query"]}}}]
        chat_messages = [{"role": "system", "content": system_prompt}] + get_safe_history(history)

        # מדידת זמן LLM סבב 1
        llm_t0 = time.perf_counter()
        chat = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=chat_messages,
            temperature=0.4,
            frequency_penalty=0.3, 
            max_tokens=150,
            tools=tools,
            tool_choice="auto"
        )
        log_event(call_id, "groq_llm_pass_1", duration_sec=round(time.perf_counter() - llm_t0, 3))

        response_message = chat.choices[0].message
        
        if response_message.tool_calls:
            tool_calls_dict = []
            for t in response_message.tool_calls:
                tool_calls_dict.append({
                    "id": t.id, "type": "function", "function": {"name": t.function.name, "arguments": t.function.arguments}
                })
                
            history.append({"role": "assistant", "content": response_message.content, "tool_calls": tool_calls_dict})

            for tool_call in response_message.tool_calls:
                if tool_call.function.name == "google_search":
                    args = json.loads(tool_call.function.arguments)
                    search_result = perform_google_search(call_id, args["query"])
                    history.append({"role": "tool", "tool_call_id": tool_call.id, "name": "google_search", "content": search_result})
            
            chat_messages = [{"role": "system", "content": system_prompt}] + get_safe_history(history)
            
            llm_t1 = time.perf_counter()
            chat = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=chat_messages, temperature=0.4, frequency_penalty=0.3, max_tokens=150
            )
            log_event(call_id, "groq_llm_pass_2", duration_sec=round(time.perf_counter() - llm_t1, 3))
            
            ai_reply = chat.choices[0].message.content.strip()
            history.append({"role": "assistant", "content": ai_reply})
        
        else:
            ai_reply = response_message.content.strip()
            history.append({"role": "assistant", "content": ai_reply})

        save_chat_data(caller_id, history, known_name)

        log_event(call_id, "request_completed", total_duration_sec=round(time.perf_counter() - req_t0, 3))
        return f"read=t-{clean_text(ai_reply)}={RECORD_COMMAND}", 200

    except Exception as e:
        log_event(call_id, "global_error", error=str(e))
        return f"read=t-סליחה תקלה זמנית בעיבוד הנתונים אנא נסו שוב בשנית={RECORD_COMMAND}", 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)

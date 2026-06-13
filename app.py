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
GEMINI_API_KEY_2 = os.environ.get("GEMINI_API_KEY_2")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GOOGLE_SCRIPT_URL = os.environ.get("GOOGLE_SCRIPT_URL")
TARGET_EMAIL = os.environ.get("TARGET_EMAIL")

# מודלים מעודכנים ויציבים - ה-8B נבחר כגיבוי החינמי העמיד ביותר מפני חסימות מכסה
MODEL_NAME = "gemini-2.5-flash"
GROQ_CHAT_MODEL = "llama-3.1-8b-instant"
GROQ_WHISPER_MODEL = "whisper-large-v3-turbo"

# פונקציית לוגים אמינה שמדפיסה מיד למסוף של Render
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

def clean_html_markdown(text):
    if not text: 
        return ""
    text = re.sub(r'```html|
```', '', text)
    return text.strip()

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
    gemini_keys = [k for k in [GEMINI_API_KEY, GEMINI_API_KEY_2] if k]
    text_log = "\n".join([f"{msg['role']}: {msg.get('content', '')}" for msg in history if 'tool_calls' not in msg])
    
    prompt = "סכם את השיחה הטלפונית הבאה בנקודות קצרות וברורות בעברית. החזר אך ורק קוד HTML נקי המשתמש בתגיות <ul> ו-<li> ללא תגיות html או body חיצוניות וללא סימוני קוד של מפתחים."

    for k in gemini_keys:
        try:
            local_client = genai.Client(api_key=k)
            response = local_client.models.generate_content(
                model=MODEL_NAME,
                contents=f"{prompt}\n\n{text_log}"
            )
            return clean_html_markdown(response.text)
        except:
            continue
            
    if GROQ_API_KEY:
        try:
            gemma_url = "https://api.groq.com/openai/v1/chat/completions"
            payload = {
                "model": GROQ_CHAT_MODEL,
                "messages": [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": text_log}
                ]
            }
            res = session.post(gemma_url, headers={"Authorization": f"Bearer {GROQ_API_KEY}"}, json=payload, timeout=15)
            if res.status_code != 200:
                log_event(call_id, "groq_summary_failed_body", status=res.status_code, body=res.text)
            res.raise_for_status()
            return clean_html_markdown(res.json()['choices'][0]['message']['content'])
        except: pass
        
    return "<ul><li>לא ניתן היה להפיק תקציר אוטומטי עבור שיחה זו</li></ul>"

def send_summary_email(call_id, caller_id, history_copy, name):
    if not GOOGLE_SCRIPT_URL or not TARGET_EMAIL: return
    try:
        raw_summary_html = generate_smart_summary(call_id, history_copy)
        
        transcript_elements = []
        for msg in history_copy:
            if 'tool_calls' in msg or msg.get('role') == 'system':
                continue
            role = msg.get('role')
            content = msg.get('content', '')
            
            if role == 'user':
                label = "👤 משתמש"
                css_class = "msg-user"
                if content == "[קובץ שמע]":
                    content = "<i>🎙️ הודעה קולית (התקבלה במערכת)</i>"
            else:
                label = "🤖 נועם (עוזר קולי)"
                css_class = "msg-assistant"
            
            elem = f'''
            <div class="msg-row">
                <div class="msg {css_class}">
                    <div class="role-label">{label}</div>
                    <div>{content}</div>
                </div>
            </div>
            '''
            transcript_elements.append(elem)
            
        transcript_html = "\n".join(transcript_elements)
        display_name = name or caller_id

        full_email_body = f'''
        <!DOCTYPE html>
        <html lang="he" dir="rtl">
        <head>
            <meta charset="UTF-8">
            <style>
                body {{ font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background-color: #f4f6f9; color: #333; margin: 0; padding: 20px; direction: rtl; text-align: right; }}
                .container {{ max-width: 650px; background: #ffffff; margin: 0 auto; padding: 30px; border-radius: 12px; box-shadow: 0 4px 15px rgba(0,0,0,0.05); border-top: 6px solid #4A90E2; }}
                .header {{ text-align: center; border-bottom: 2px solid #eaedf2; padding-bottom: 20px; margin-bottom: 25px; }}
                .header h1 {{ color: #2C3E50; margin: 0; font-size: 24px; }}
                .meta-info {{ font-size: 13px; color: #7f8c8d; margin-top: 8px; }}
                .section-title {{ font-size: 18px; color: #2980b9; margin-top: 30px; margin-bottom: 15px; border-right: 4px solid #2980b9; padding-right: 10px; font-weight: bold; }}
                .summary-box {{ background-color: #f8f9fa; border-right: 4px solid #2ecc71; padding: 15px 20px; border-radius: 6px; line-height: 1.6; font-size: 15px; }}
                .summary-box ul {{ margin: 0; padding-right: 20px; }}
                .summary-box li {{ margin-bottom: 8px; }}
                .transcript-container {{ margin-top: 20px; width: 100%; }}
                .msg-row {{ width: 100%; clear: both; display: block; margin-bottom: 15px; }}
                .msg {{ padding: 12px 15px; border-radius: 8px; line-height: 1.5; max-width: 75%; box-sizing: border-box; }}
                .msg-user {{ background-color: #e8f4fd; border-right: 4px solid #3498db; float: right; text-align: right; }}
                .msg-assistant {{ background-color: #f0f4f1; border-right: 4px solid #27ae60; float: left; text-align: right; }}
                .role-label {{ font-weight: bold; font-size: 12px; margin-bottom: 5px; color: #555; }}
                .footer {{ text-align: center; font-size: 12px; color: #bdc3c7; margin-top: 40px; border-top: 1px solid #eaedf2; padding-top: 15px; clear: both; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h1>📋 סיכום שיחה טלפונית - נועם עוזר קולי</h1>
                    <div class="meta-info">מתקשר: <strong>{display_name}</strong> | מזהה שיחה: {call_id}</div>
                </div>
                
                <div class="section-title">📌 תקציר המנהלים (סיכום השיחה)</div>
                <div class="summary-box">
                    {raw_summary_html}
                </div>
                
                <div class="section-title">💬 תמלול מהלך השיחה</div>
                <div class="transcript-container">
                    {transcript_html}
                    <div style="clear: both;"></div>
                </div>
                
                <div class="footer">
                    נשלח אוטומטית על ידי מערכת ה-AI של נועם העוזר הקולי
                </div>
            </div>
        </body>
        </html>
        '''

        session.post(GOOGLE_SCRIPT_URL, json={"to": TARGET_EMAIL, "subject": f"סיכום שיחה מפורט - {display_name}", "htmlBody": full_email_body}, timeout=10)
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
        return Response(f"read=t-שלום כאן נועם אנא דברו לאחר הצליל={RECORD_COMMAND}", mimetype='text/plain')

    try:
        log_event(call_id, "downloading_audio_file", path=audio_path[-1])
        audio_res = session.get("https://www.call2all.co.il/ym/api/DownloadFile", params={"token": YEMOT_TOKEN, "path": f"ivr2:{audio_path[-1]}"}, timeout=20)
        audio_res.raise_for_status()
        
        system_prompt = (
            "You are Noam, a helpful voice assistant on a phone call. "
            "Respond warmly and ELABORATELY in Hebrew. Provide long, comprehensive, and detailed answers. "
            "Expand significantly on the requested topics instead of being brief or concise. "
            "INTERNAL KNOWLEDGE FIRST: Always answer directly from your own internal knowledge base first. "
            "ONLY execute a 'wikipedia_search' tool call if you genuinely do not know the answer from your knowledge base, or to prevent hallucination. "
            "CRITICAL FORMAT RULE: Do NOT use any punctuation marks whatsoever in your text output to the user. No periods, no commas, no hyphens, no question marks. "
            "Use only clear Hebrew letters and spaces. "
            "NEVER output your internal reasoning, thoughts, or any English monologues to the user."
        )
        
        contents = [types.Content(role='user' if h['role'] == 'user' else 'model', parts=[types.Part(text=h['content'])]) for h in history]
        contents.append(types.Content(role="user", parts=[
            types.Part.from_bytes(data=audio_res.content, mime_type="audio/wav"),
            types.Part(text="הקשב לקובץ השמע המצורף וענה למשתמש בעברית תשובה ארוכה ומפורטת ללא סימני פיסוק כלל.")
        ]))

        gemini_keys = [k for k in [GEMINI_API_KEY, GEMINI_API_KEY_2] if k]
        response_text = None
        user_content_for_history = "[קובץ שמע]"

        # שלב 1: ניסיון מול מפתחות ג'מיני הזמינים
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

                response_text = response.text
                log_event(call_id, f"gemini_key_{idx+1}_success")
                break
            except Exception as gemini_err:
                log_event(call_id, f"gemini_key_{idx+1}_failed", error=str(gemini_err))
                continue

        # שלב 2: פתרון קצה (Fallback) במידה וג'מיני נכשלו - מעבר ל-Groq עם מודל ה-8B העמיד
        if not response_text:
            if GROQ_API_KEY:
                try:
                    log_event(call_id, "groq_fallback_triggered")
                    
                    log_event(call_id, "groq_whisper_transcription_started")
                    whisper_url = "https://api.groq.com/openai/v1/audio/transcriptions"
                    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
                    files = {"file": ("audio.wav", audio_res.content, "audio/wav")}
                    data = {"model": GROQ_WHISPER_MODEL}
                    
                    whisper_res = session.post(whisper_url, headers=headers, files=files, data=data, timeout=15)
                    whisper_res.raise_for_status()
                    user_transcription = whisper_res.json().get("text", "")
                    log_event(call_id, "groq_whisper_success", text=user_transcription)
                    
                    user_content_for_history = f"🎙️ {user_transcription}"
                    
                    log_event(call_id, "groq_chat_generation_started")
                    chat_url = "https://api.groq.com/openai/v1/chat/completions"
                    
                    messages = [{"role": "system", "content": system_prompt}]
                    for h in history:
                        messages.append({"role": h['role'], "content": h['content']})
                    messages.append({"role": "user", "content": user_transcription})
                    
                    payload = {
                        "model": GROQ_CHAT_MODEL,
                        "messages": messages
                    }
                    
                    chat_res = session.post(chat_url, headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}, json=payload, timeout=15)
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
        
        history.extend([{"role": "user", "content": user_content_for_history}, {"role": "assistant", "content": ai_reply}])
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
            friendly_message = "החיבור לשרתי השרות נתקע עקב בעיית תקשורת זמנית אנא נסו שוב"
        else:
            friendly_message = "חלקה שגיאה טכנית זמנית בעיבוד הנתונים של השיחה אנא נסו שוב מאוחר יותר"
            
        friendly_message = clean_text(friendly_message)
        return Response(f"read=t-{friendly_message}", mimetype='text/plain')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)

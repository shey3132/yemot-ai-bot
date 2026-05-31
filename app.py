import os
import io
import json
import sqlite3
import logging
import re
import html
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, request
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from groq import Groq
from pydub import AudioSegment
from pydub.silence import detect_nonsilent

app = Flask(__name__)

# =====================
# CONFIG & LOGGING
# =====================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

YEMOT_TOKEN = os.environ.get("YEMOT_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")
GOOGLE_CX = os.environ.get("GOOGLE_CX")
GOOGLE_SCRIPT_URL = os.environ.get("GOOGLE_SCRIPT_URL")

CUSTOM_WHISPER_URL = os.environ.get("CUSTOM_WHISPER_URL") 

client = Groq(api_key=GROQ_API_KEY)
RECORD_CMD = "user_audio,no,record,,,yes,yes,no,2,60"
DB = "chat.db"

executor = ThreadPoolExecutor(max_workers=5)

session = requests.Session()
retry = Retry(total=2, backoff_factor=0.3, status_forcelist=[500, 502, 503, 504])
session.mount("https://", HTTPAdapter(max_retries=retry))

def log_event(call_id, event, error=""):
    msg = f"Call: {call_id} | Event: {event}"
    if error: msg += f" | Error: {error}"
    logger.info(msg)

# =====================
# DATABASE
# =====================
def init_db():
    with closing(sqlite3.connect(DB)) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS chat (user TEXT PRIMARY KEY, history TEXT)")
init_db()

def load(user):
    with closing(sqlite3.connect(DB)) as conn:
        cur = conn.cursor()
        cur.execute("SELECT history FROM chat WHERE user=?", (user,))
        r = cur.fetchone()
        if r: return json.loads(r[0])
    return []

def save(user, hist):
    with closing(sqlite3.connect(DB)) as conn:
        conn.execute("INSERT INTO chat(user, history) VALUES(?, ?) ON CONFLICT(user) DO UPDATE SET history=excluded.history", (user, json.dumps(hist[-30:])))

def delete_history(user):
    with closing(sqlite3.connect(DB)) as conn:
        conn.execute("DELETE FROM chat WHERE user=?", (user,))
        conn.commit()
    logger.info(f"History deleted for user: {user}")

# =====================
# AUDIO PROCESSING
# =====================
def process_audio(audio_bytes):
    try:
        audio = AudioSegment.from_wav(io.BytesIO(audio_bytes))
        audio = audio.normalize()
        nonsilent_ranges = detect_nonsilent(audio, min_silence_len=500, silence_thresh=audio.dBFS-16)
        if nonsilent_ranges:
            start_trim = nonsilent_ranges[0][0]
            end_trim = nonsilent_ranges[-1][1]
            audio = audio[start_trim:end_trim]
            
        out_io = io.BytesIO()
        audio.export(out_io, format="wav")
        return out_io.getvalue()
    except Exception as e:
        logger.error(f"Audio processing error, returning original: {e}")
        return audio_bytes

# =====================
# EMAIL & SUMMARY LOGIC (GOOGLE APPS SCRIPT)
# =====================
def generate_smart_summary(call_id, history_copy):
    text = "\n".join(
        f"{m['role']}: {m.get('content','')}"
        for m in history_copy if m['role'] != 'system'
    )
    res = client.chat.completions.create(
        model="llama-3.1-8b-instant",
        messages=[
            {"role": "system", "content": "סכם את השיחה הזו ל-3 נקודות קצרות בלבד, בשפה עניינית."},
            {"role": "user", "content": text}
        ],
        temperature=0.3,
        max_tokens=200
    )
    return res.choices[0].message.content.strip()

def send_summary_email(call_id, caller_id, history_copy, name):
    # שימוש בכתובת גיבוי במידה והמשתנה חסר ב-Render, כדי שהקוד לעולם לא יעצור
    target = os.environ.get("TARGET_EMAIL") or "test@example.com"
    
    if not GOOGLE_SCRIPT_URL or not history_copy:
        logger.warning(f"Email skipped: Missing URL or empty history. Call ID: {call_id}")
        return
        
    try:
        # הורדנו את מגבלת 5 ההודעות - עכשיו מסכם תמיד (החל מהודעה 1)
        raw_summary = generate_smart_summary(call_id, history_copy) if len(history_copy) >= 1 else "שיחה קצרה מדי."
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
            if msg["role"] == "system": continue
            role_display = "משתמש" if msg["role"] == "user" else "מערכת" if msg["role"] == "tool" else "נועם"
            color_bg = "#e0f2fe" if msg["role"] == "user" else "#fef3c7" if msg["role"] == "tool" else "#f1f5f9"
            color_border = "#0ea5e9" if msg["role"] == "user" else "#f59e0b" if msg["role"] == "tool" else "#64748b"
            
            if "tool_calls" not in msg:
                content = html.escape(msg.get("content", ""))
                body += f'<div style="margin-bottom: 15px; padding: 12px 15px; background-color: {color_bg}; border-right: 4px solid {color_border}; border-radius: 4px;"><strong>{role_display}:</strong><br>{content}</div>'
        
        body += "</div></div>"
        
        # שליחה לאפפ סקריפט שלך
        response = session.post(GOOGLE_SCRIPT_URL, json={"to": target, "subject": subject, "htmlBody": body}, timeout=15)
        response.raise_for_status() 
        log_event(call_id, "email_sent_success", f"Sent to {target}")
        
    except Exception as e:
        log_event(call_id, "email_send_error", error=str(e))

# =====================
# TOOLS (APIs)
# =====================
def clean(text):
    cleaned = re.sub(r'[\.\-\=&\,\'\"!\?]', ' ', text)
    return " ".join(cleaned.split())

def google_search(query):
    if not GOOGLE_API_KEY or not GOOGLE_CX: return None
    try:
        r = session.get("https://www.googleapis.com/customsearch/v1", params={"q": query, "key": GOOGLE_API_KEY, "cx": GOOGLE_CX}, timeout=8)
        if r.status_code != 200: return None
        items = r.json().get("items", [])
        if not items: return "אין תוצאות"
        return " | ".join(f"{i['title']} {i['snippet']}" for i in items[:2])
    except: return None

def get_bus_realtime(station_code):
    try:
        url = "https://open-bus-stride-api.hasadna.org.il/siri_rides/list"
        params = {"limit": 3, "siri_route__line_refs": station_code}
        r = session.get(url, params=params, timeout=8)
        if r.status_code != 200: return None
        data = r.json()
        if not data: return f"API ERROR לא נמצאו נתונים כלל עבור התחנה {station_code}"
        return f"DATA_FOUND נתונים עבור התחנה {station_code} {str(data[:3])}"
    except: return None

def find_route_or_stop(search_text):
    try:
        line_numbers = re.findall(r'\b\d{1,3}\b', search_text)
        url = "https://open-bus-stride-api.hasadna.org.il/gtfs_routes/list"
        params = {"limit": 3}
        if line_numbers: params["line_refs"] = line_numbers[0]
        r = session.get(url, params=params, timeout=8)
        if r.status_code != 200: return None
        data = r.json()
        if not data: return f"API ERROR לא נמצא מידע עבור {search_text}"
        return f"DATA_FOUND מידע מה API {str(data[:2])}"
    except: return None

# =====================
# MAIN FLOW
# =====================
@app.route("/ai-chat", methods=["GET", "POST"])
def ai_chat():
    caller_id = request.values.get("ApiPhone", "unknown")
    call_id = request.values.get("ApiCallId", "0")
    
    # טעינה בטוחה
    history = load(caller_id)
    
    # טיפול בניתוק
    if request.values.get("hangup") == "yes":
        if history: # שולח מייל רק אם יש היסטוריה בזיכרון
            executor.submit(send_summary_email, call_id, caller_id, history, "מתקשר " + caller_id)
            delete_history(caller_id)
        return "noop", 200
        
    logger.info(f"--- New Request from {caller_id} ---")
    
    # ... (שאר הקוד של עיבוד האודיו והתמלול נשאר אותו דבר) ...
    
    # אחרי הוספת התשובה להיסטוריה:
    history.append({"role": "user", "content": text})
    # ... (לוגיקה של ה-LLM) ...
    history.append({"role": "assistant", "content": answer})
    
    # שמירה ל-DB
    save(caller_id, history)
    
    return f"read=t-{clean(answer)}={RECORD_CMD}", 200
        
    logger.info(f"--- New Request from {caller_id} ---")
    history = load(caller_id)

    audio = request.values.getlist("user_audio")
    if not audio:
        return f"read=t-שלום אני נועם העוזר הקולי שלך במה אפשר לעזור={RECORD_CMD}", 200

    try:
        path = f"ivr2:{audio[-1]}"
        res = session.get("https://www.call2all.co.il/ym/api/DownloadFile", params={"token": YEMOT_TOKEN, "path": path}, timeout=20)
        res.raise_for_status()
    except:
        return f"read=t-שגיאה בהורדת הקלטה נסה שוב={RECORD_CMD}", 200

    processed_audio_bytes = process_audio(res.content)

    try:
        if CUSTOM_WHISPER_URL:
            files = {'file': ('audio.wav', processed_audio_bytes, 'audio/wav')}
            tr_res = requests.post(CUSTOM_WHISPER_URL, files=files, timeout=10)
            text = tr_res.json().get("text", "").strip()
        else:
            tr = client.audio.transcriptions.create(
                file=("audio.wav", processed_audio_bytes), 
                model="whisper-large-v3-turbo", 
                language="he"
            )
            text = tr.text.strip()
            
        logger.info(f"User said: '{text}'")
    except Exception as e:
        logger.error(f"Transcription failed: {e}")
        return f"read=t-לא הצלחתי להבין נסה שוב={RECORD_CMD}", 200

    # הגנה פונטית: חסימת טקסט באנגלית מלהגיע ל-LLM ולהזות
    if re.search(r'[a-zA-Z]{2,}', text):
        logger.warning(f"Detected English phonetic gibberish: '{text}'. Blocking from LLM.")
        return f"read=t-סליחה לא שמעתי ברור מאיזו עיר לאיזו עיר תרצה לנסוע={RECORD_CMD}", 200

    history.append({"role": "user", "content": text})

    # ניתוב חכם לחיפוש עם זיהוי מספרים כקווים
    bus_result = None
    search_result = None

    station_match = re.search(r'\b\d{5}\b', text)
    line_match = re.search(r'\b\d{1,3}\b', text) # מזהה מספרים בודדים או תלת ספרתיים
    is_bus_query = any(k in text for k in ["אוטובוס", "קו", "תחנה", "מגיע", "רכבת", "תחבורה"])
    is_general_query = any(k in text for k in ["מה", "איך", "מי", "איפה", "למה", "מתי"])

    if station_match:
        bus_result = get_bus_realtime(station_match.group())
    elif is_bus_query or line_match:
        bus_result = find_route_or_stop(text)
    elif is_general_query:
        search_result = google_search(text)

    try:
        # הקשחת ה-System Prompt נגד הזיות
        system_instruction = (
            "אתה נועם עוזר קולי אישי חכם וידידותי למענה טלפוני. "
            "ענה בטבעיות, בקצרה ולעניין. אל תשתמש כלל בסימני פיסוק או תווים מיוחדים. "
            "אם המשתמש שואל על קו אוטובוס או עיר ולא נמצא מידע במערכת אל תמציא מידע בשום אופן. "
            "פשוט אמור 'לא מצאתי את הקו, מאיזו עיר לאיזו עיר תרצה לנסוע?'. "
            "אם המשפט של המשתמש קטוע או לא הגיוני בקש ממנו לחזור שוב בבירור."
        )
        
        messages = [{"role": "system", "content": system_instruction}] + history

        if bus_result:
            messages.append({"role": "system", "content": f"מידע מהמערכת {bus_result} ענה רק על בסיס זה אל תמציא קווים"})
        elif search_result:
            messages.append({"role": "system", "content": f"מידע עדכני מחיפוש בגוגל {search_result} נסח תשובה קצרה למשתמש"})

        res_llm = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=messages,
            temperature=0.4
        )

        answer = res_llm.choices[0].message.content.strip()
        logger.info(f"LLM Response: '{answer}'")

    except Exception as e:
        logger.error(f"LLM failed: {e}")
        answer = "יש תקלה זמנית נסה שוב"

    history.append({"role": "assistant", "content": answer})
    save(caller_id, history)

    clean_answer = clean(answer)
    final_read = f"read=t-{clean_answer}={RECORD_CMD}"
    return final_read, 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)

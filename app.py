import os
import json
import time
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
TARGET_EMAIL = os.environ.get("TARGET_EMAIL")

client = Groq(api_key=GROQ_API_KEY)

# *** תיקון קריטי: פקודת ההקלטה המדויקת לפי ההוראות ***
# ParameterName=user_audio, no, record, Path=, FileName=, NoMenu=yes, SaveHangup=yes, Append=no, MinLen=2, MaxLen=60
RECORD_CMD = "user_audio,no,record,,,yes,yes,no,2,60"

DB = "chat.db"

executor = ThreadPoolExecutor(max_workers=5)

session = requests.Session()
retry = Retry(total=2, backoff_factor=0.3, status_forcelist=[500, 502, 503, 504])
session.mount("https://", HTTPAdapter(max_retries=retry))

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
# EMAIL & SUMMARY LOGIC
# =====================
def generate_summary(client_instance, history):
    text = "\n".join(
        f"{m['role']}: {m.get('content','')}"
        for m in history if m['role'] != 'system'
    )
    res = client_instance.chat.completions.create(
        model="llama-3.1-8b-instant",
        messages=[
            {"role": "system", "content": "סכם את השיחה הזו ל-3 נקודות קצרות בלבד, בשפה עניינית."},
            {"role": "user", "content": text}
        ],
        temperature=0.3,
        max_tokens=200
    )
    return res.choices[0].message.content.strip()

def send_email_summary(call_id, caller_id, history, name, client_instance):
    try:
        if not history or len(history) < 2: return
        summary = generate_summary(client_instance, history)
        safe_summary = html.escape(summary)
        safe_name = html.escape(name or "לא ידוע")
        safe_phone = html.escape(caller_id or "לא ידוע")

        subject = f"סיכום שיחה ממענה חכם - {safe_name} ({safe_phone})"
        body = f"""<div style="font-family: Arial; direction: rtl;">
            <h2>סיכום שיחה במערכת</h2>
            <p><b>משתמש:</b> {safe_name} | <b>טלפון:</b> {safe_phone}</p>
            <h3>סיכום:</h3><p>{safe_summary.replace(chr(10), '<br>')}</p><hr><h3>תמלול מלא:</h3>"""

        for msg in history:
            role = msg["role"]
            if role == "system": continue
            content = html.escape(msg.get("content", ""))
            color = "#e3f2fd" if role == "user" else "#f5f5f5"
            sender_name = "משתמש" if role == "user" else "עוזר קולי"
            body += f"<div style='background:{color}; margin:10px 0; padding:10px; border-radius:5px;'><b>{sender_name}:</b> {content}</div>"

        body += "</div>"
        
        if GOOGLE_SCRIPT_URL and TARGET_EMAIL:
            requests.post(GOOGLE_SCRIPT_URL, json={"to": TARGET_EMAIL, "subject": subject, "htmlBody": body}, timeout=10)
    except Exception as e:
        logger.error(f"EMAIL ERROR: {e}")

# =====================
# TOOLS (APIs)
# =====================
def clean(text):
    # *** תיקון קריטי: ניקוי אגרסיבי של כל סימן העלול להפיל את המערכת ***
    # מסירים: נקודה, פסיק, מקף, שווה, אמפרסנד, גרש, מרכאות
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
    
    # 📴 טיפול בסיום שיחה - HANGUP
    if request.values.get("hangup") == "yes":
        history = load(caller_id)
        executor.submit(send_email_summary, call_id, caller_id, history.copy(), "מתקשר " + caller_id, client)
        delete_history(caller_id)
        return "noop", 200
        
    logger.info(f"--- New Request from {caller_id} ---")
    history = load(caller_id)

    # 1. NO AUDIO → ASK TO RECORD
    audio = request.values.getlist("user_audio")
    if not audio:
        # *** תיקון קריטי: פקודת פתיחה מדויקת לפי הפורמט ***
        return f"read=t-שלום אני העוזר הקולי שלך במה אפשר לעזור={RECORD_CMD}", 200

    # 2. DOWNLOAD AUDIO
    try:
        path = f"ivr2:{audio[-1]}"
        res = session.get("https://www.call2all.co.il/ym/api/DownloadFile", params={"token": YEMOT_TOKEN, "path": path}, timeout=20)
        res.raise_for_status()
    except:
        return f"read=t-שגיאה בהורדת הקלטה נסה שוב={RECORD_CMD}", 200

    # 3. TRANSCRIBE
    try:
        tr = client.audio.transcriptions.create(file=("audio.wav", res.content), model="whisper-large-v3-turbo", language="he")
        text = tr.text.strip()
        logger.info(f"User said: '{text}'")
    except:
        return f"read=t-לא הצלחתי להבין נסה שוב={RECORD_CMD}", 200

    history.append({"role": "user", "content": text})

    # =====================
    # 4. SMART ROUTING (TOOLS)
    # =====================
    bus_result = None
    search_result = None

    station_match = re.search(r'\b\d{5}\b', text)
    is_bus_query = any(k in text for k in ["אוטובוס", "קו", "תחנה", "מגיע", "רכבת", "תחבורה"])
    is_general_query = any(k in text for k in ["מה", "איך", "מי", "איפה", "למה", "מתי"])

    if station_match:
        bus_result = get_bus_realtime(station_match.group())
    elif is_bus_query:
        bus_result = find_route_or_stop(text)
    elif is_general_query:
        search_result = google_search(text)

    # =====================
    # 5. LLM RESPONSE
    # =====================
    try:
        # חוקי בסיס פשוטים ופתוחים - מחמיר על חוסר סימני פיסוק
        system_instruction = "אתה עוזר קולי אישי חכם וידידותי למענה טלפוני ענה בטבעיות בקצרה ולעניין אל תשתמש כלל בסימני פיסוק או תווים מיוחדים"
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

    # =====================
    # 6. RETURN
    # =====================
    # מנקים את הטקסט לפני השליחה חזרה לימות המשיח
    clean_answer = clean(answer)
    final_read = f"read=t-{clean_answer}={RECORD_CMD}"
    logger.info(f"Returning: {final_read}")
    return final_read, 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)

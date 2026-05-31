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

# --- הגדרות ולוגים ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

YEMOT_TOKEN = os.environ.get("YEMOT_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")
GOOGLE_CX = os.environ.get("GOOGLE_CX")
GOOGLE_SCRIPT_URL = os.environ.get("GOOGLE_SCRIPT_URL")
CUSTOM_WHISPER_URL = os.environ.get("CUSTOM_WHISPER_URL") 
TARGET_EMAIL = os.environ.get("TARGET_EMAIL")

client = Groq(api_key=GROQ_API_KEY)
RECORD_CMD = "user_audio,no,record,,,yes,yes,no,2,60"
DB = "chat.db"
executor = ThreadPoolExecutor(max_workers=5)
session = requests.Session()
session.mount("https://", HTTPAdapter(max_retries=Retry(total=2)))

# --- בסיס נתונים ---
def init_db():
    with closing(sqlite3.connect(DB)) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS chat (user TEXT PRIMARY KEY, history TEXT)")
init_db()

def load(user):
    with closing(sqlite3.connect(DB)) as conn:
        cur = conn.cursor()
        cur.execute("SELECT history FROM chat WHERE user=?", (user,))
        r = cur.fetchone()
        return json.loads(r[0]) if r else []

def save(user, hist):
    with closing(sqlite3.connect(DB)) as conn:
        conn.execute("INSERT INTO chat(user, history) VALUES(?, ?) ON CONFLICT(user) DO UPDATE SET history=excluded.history", (user, json.dumps(hist[-30:])))

def delete_history(user):
    with closing(sqlite3.connect(DB)) as conn:
        conn.execute("DELETE FROM chat WHERE user=?", (user,))
        conn.commit()

# --- עיבוד אודיו וכלי עזר ---
def process_audio(audio_bytes):
    try:
        audio = AudioSegment.from_wav(io.BytesIO(audio_bytes)).normalize()
        ranges = detect_nonsilent(audio, min_silence_len=500, silence_thresh=audio.dBFS-16)
        if ranges: audio = audio[ranges[0][0]:ranges[-1][1]]
        out = io.BytesIO()
        audio.export(out, format="wav")
        return out.getvalue()
    except: return audio_bytes

def get_bus_realtime(station_code):
    try:
        r = session.get("https://open-bus-stride-api.hasadna.org.il/siri_rides/list", params={"limit": 3, "siri_route__line_refs": station_code}, timeout=8)
        return str(r.json()) if r.status_code == 200 else "אין נתונים"
    except: return "שגיאה ב-API"

def google_search(query):
    try:
        r = session.get("https://www.googleapis.com/customsearch/v1", params={"q": query, "key": GOOGLE_API_KEY, "cx": GOOGLE_CX}, timeout=8)
        items = r.json().get("items", [])
        return " | ".join(f"{i['title']} {i['snippet']}" for i in items[:2]) if items else "אין תוצאות"
    except: return "שגיאת חיפוש"

def send_summary_email(call_id, caller_id, history_copy, name):
    url = os.environ.get("GOOGLE_SCRIPT_URL")
    if not url or not history_copy: return
    try:
        summary_text = "סיכום שיחה אוטומטי"
        body = f"סיכום עבור {name}: <br>".join([f"{m['role']}: {m['content']}" for m in history_copy])
        session.post(url, json={"to": TARGET_EMAIL, "subject": f"סיכום שיחה {caller_id}", "htmlBody": body}, timeout=15)
    except Exception as e: logger.error(f"Email error: {e}")

# --- פונקציה ראשית ---
@app.route("/ai-chat", methods=["GET", "POST"])
def ai_chat():
    caller_id = request.values.get("ApiPhone", "unknown")
    call_id = request.values.get("ApiCallId", "0")
    history = load(caller_id)

    if request.values.get("hangup") == "yes":
        if history:
            executor.submit(send_summary_email, call_id, caller_id, history, caller_id)
            delete_history(caller_id)
        return "noop", 200

    audio_list = request.values.getlist("user_audio")
    if not audio_list:
        return f"read=t-שלום אני נועם העוזר הקולי, במה אפשר לעזור={RECORD_CMD}", 200

    text = "לא הבנתי"
    try:
        res = session.get("https://www.call2all.co.il/ym/api/DownloadFile", params={"token": YEMOT_TOKEN, "path": f"ivr2:{audio_list[-1]}"}, timeout=20)
        audio_data = process_audio(res.content)
        
        if CUSTOM_WHISPER_URL:
            tr_res = requests.post(CUSTOM_WHISPER_URL, files={'file': ('audio.wav', audio_data)}, timeout=10)
            text = tr_res.json().get("text", "")
        else:
            tr = client.audio.transcriptions.create(file=("audio.wav", audio_data), model="whisper-large-v3-turbo", language="he")
            text = tr.text.strip()
    except Exception as e:
        logger.error(f"Transcription Error: {e}")
        return f"read=t-לא שמעתי היטב נסה שוב={RECORD_CMD}", 200

    answer = "סליחה, אני לא מבין, תוכל לחזור על זה?"
    try:
        history.append({"role": "user", "content": text})
        
        bus_res = get_bus_realtime(re.search(r'\d{3,5}', text).group()) if re.search(r'\d{3,5}', text) else None
        search_res = google_search(text) if not bus_res else None
        
        system_msg = "אתה נועם עוזר קולי ענה קצרות ובלי סימני פיסוק."
        if bus_res: system_msg += f" מידע תחבורה: {bus_res}"
        if search_res: system_msg += f" מידע מהאינטרנט: {search_res}"
        
        res_llm = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "system", "content": system_msg}] + history,
            temperature=0.4
        )
        answer = res_llm.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"LLM Error: {e}")

    history.append({"role": "assistant", "content": answer})
    save(caller_id, history)
    
    clean_text = re.sub(r'[^\w\s]', '', answer)
    return f"read=t-{clean_text}={RECORD_CMD}", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)

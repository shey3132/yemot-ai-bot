import os
import json
import time
import sqlite3
import logging
import re
from contextlib import closing
from flask import Flask, request
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from groq import Groq

app = Flask(__name__)

# =====================
# CONFIG & LOGGING
# =====================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

YEMOT_TOKEN = os.environ.get("YEMOT_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")
GOOGLE_CX = os.environ.get("GOOGLE_CX")

client = Groq(api_key=GROQ_API_KEY)

RECORD_CMD = "user_audio,no,record,,,yes,yes,no,1,120"
DB = "chat.db"

# =====================
# HTTP SESSION
# =====================
session = requests.Session()
retry = Retry(total=2, backoff_factor=0.3, status_forcelist=[500, 502, 503, 504])
session.mount("https://", HTTPAdapter(max_retries=retry))

# =====================
# DATABASE
# =====================
def init_db():
    with closing(sqlite3.connect(DB)) as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS chat (
            user TEXT PRIMARY KEY,
            history TEXT
        )
        """)
init_db()

def load(user):
    with closing(sqlite3.connect(DB)) as conn:
        cur = conn.cursor()
        cur.execute("SELECT history FROM chat WHERE user=?", (user,))
        r = cur.fetchone()
        if r:
            return json.loads(r[0])
    return []

def save(user, hist):
    with closing(sqlite3.connect(DB)) as conn:
        conn.execute("""
        INSERT INTO chat(user, history)
        VALUES(?, ?)
        ON CONFLICT(user) DO UPDATE SET history=excluded.history
        """, (user, json.dumps(hist[-20:])))

# =====================
# CLEAN TEXT FOR YEMOT
# =====================
def clean(text):
    cleaned = text.replace(".", " ").replace("-", " ").replace("=", " ").replace("&", " ")
    return " ".join(cleaned.split())

# =====================
# GOOGLE SEARCH API
# =====================
def google_search(query):
    if not GOOGLE_API_KEY or not GOOGLE_CX:
        logger.warning("Google API keys are missing.")
        return None
    try:
        logger.info(f"Searching Google for: {query}")
        r = session.get(
            "https://www.googleapis.com/customsearch/v1",
            params={"q": query, "key": GOOGLE_API_KEY, "cx": GOOGLE_CX},
            timeout=8
        )
        if r.status_code == 403:
            logger.error("Google API 403 Forbidden.")
            return None

        data = r.json()
        items = data.get("items", [])
        if not items:
            return "אין תוצאות"

        return " | ".join(f"{i['title']} - {i['snippet']}" for i in items[:2])
    except Exception as e:
        logger.error(f"Google Search failed: {e}")
        return None

# =====================
# STRIDE BUS API 1: זמני הגעה לפי מספר תחנה
# =====================
def get_bus_realtime(station_code):
    try:
        logger.info(f"Fetching realtime arrivals for station: {station_code}")
        url = "https://open-bus-stride-api.hasadna.org.il/siri_rides/list"
        params = {"limit": 3} # כאן בהמשך תכוון לפרמטר הסינון המדויק של התחנה מהדוקומנטציה
        
        r = session.get(url, params=params, timeout=8)
        if r.status_code != 200:
            logger.error(f"Stride Realtime Error: {r.status_code}")
            return None
            
        data = r.json()
        if not data:
            return f"אין אוטובוסים קרובים לתחנה {station_code} בדקות הקרובות."
            
        return f"נמצאו נתוני זמן אמת לתחנה {station_code}: {str(data[:3])}"
    except Exception as e:
        logger.error(f"Stride Realtime failed: {e}")
        return None

# =====================
# STRIDE BUS API 2: חיפוש תחנות לפי שם עיר / רחוב (NEW)
# =====================
def find_stops_by_name(search_text):
    try:
        logger.info(f"Searching for stops matching text: {search_text}")
        # פנייה לנתיב החיפוש הסטטי של התחנות (GTFS) ב-Stride
        url = "https://open-bus-stride-api.hasadna.org.il/gtfs_stops/list"
        
        # אנחנו מנסים להעביר את הטקסט כפרמטר חיפוש (יש להתאים לשם הפרמטר המדויק ב-Swagger)
        params = {
            "limit": 3
        }
        
        r = session.get(url, params=params, timeout=8)
        if r.status_code != 200:
            logger.error(f"Stride Stop Search Error: {r.status_code}")
            return None
            
        data = r.json()
        if not data:
            return f"לא נמצאו תחנות העונות לתיאור: {search_text}"
            
        return f"תוצאות חיפוש תחנות עבור '{search_text}': {str(data[:3])}"
    except Exception as e:
        logger.error(f"Stride Stop Search failed: {e}")
        return None

# =====================
# MAIN FLOW
# =====================
@app.route("/ai-chat", methods=["GET", "POST"])
def ai_chat():
    user = request.values.get("ApiPhone", "unknown")
    call_id = request.values.get("ApiCallId", "0")
    
    logger.info(f"--- New Request from {user} | Call ID: {call_id} ---")

    history = load(user)

    # 1. NO AUDIO → ASK TO RECORD
    audio = request.values.getlist("user_audio")
    if not audio:
        return f"read=t-שלום, במה אפשר לעזור?={RECORD_CMD}", 200

    # 2. DOWNLOAD AUDIO
    try:
        path = f"ivr2:{audio[-1]}"
        res = session.get(
            "https://www.call2all.co.il/ym/api/DownloadFile",
            params={"token": YEMOT_TOKEN, "path": path},
            timeout=20
        )
        res.raise_for_status()
    except Exception as e:
        logger.error(f"Download failed: {e}")
        return f"read=t-שגיאה בהורדת הקלטה נסה שוב={RECORD_CMD}", 200

    # 3. TRANSCRIBE
    try:
        tr = client.audio.transcriptions.create(
            file=("audio.wav", res.content),
            model="whisper-large-v3-turbo",
            language="he"
        )
        text = tr.text.strip()
        logger.info(f"User said: '{text}'")
    except Exception as e:
        logger.error(f"Transcription failed: {e}")
        return f"read=t-לא הצלחתי להבין את ההקלטה נסה שוב={RECORD_CMD}", 200

    history.append({"role": "user", "content": text})

    # =====================
    # 4. DECISION MAKER (BUS / GOOGLE)
    # =====================
    search_result = None
    bus_result = None

    # בדיקה האם יש מספר בן 5 ספרות (קוד תחנה)
    station_match = re.search(r'\b\d{5}\b', text)
    is_bus_query = any(k in text for k in ["אוטובוס", "קו", "תחנה", "מתי מגיע", "נסיעה", "מפעיל"])

    if station_match:
        # יש מספר תחנה -> נביא זמני הגעה בזמן אמת
        station_code = station_match.group()
        logger.info(f"Routing to: Realtime Bus Arrivals for station {station_code}")
        bus_result = get_bus_realtime(station_code)
        
    elif is_bus_query:
        # המשתמש מדבר על אוטובוסים אבל לא אמר קוד תחנה -> נחפש לפי הטקסט (שם עיר/רחוב)
        logger.info("Routing to: Bus Stop/Route Search by text description")
        bus_result = find_stops_by_name(text)
        
    elif any(k in text for k in ["מה", "איך", "מי", "איפה"]):
        # שאלה כללית -> גוגל
        logger.info("Routing to: Google Search")
        search_result = google_search(text)

    # =====================
    # 5. LLM RESPONSE
    # =====================
    try:
        # הגדרת חוקי מערכת קשיחים - כולל הגבלת הרכבות!
        system_instruction = (
            "אתה עוזר קולי קצר וברור בטלפון. אל תשתמש בסימני פיסוק מורכבים כמו נקודותיים או מקפים. "
            "חשוב מאוד: המערכת שלך תומכת ומציגה מידע על אוטובוסים בלבד! אין לך גישה לנתוני רכבת ישראל או הרכבת הקלה. "
            "אם המשתמש שואל על רכבות, הסבר לו בנימוס שאתה תומך כרגע רק באוטובוסים."
        )
        
        messages = [{"role": "system", "content": system_instruction}] + history

        if bus_result:
            messages.append({
                "role": "system",
                "content": f"מידע מעודכן ממערכת התחבורה הציבורית Open Bus Stride: {bus_result}. ענה למשתמש בצורה תמציתית וברורה על בסיס נתונים אלו."
            })
        elif search_result:
            messages.append({
                "role": "system",
                "content": f"מידע שהתקבל מחיפוש בגוגל: {search_result}. נסח תשובה קצרה."
            })

        res_llm = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=messages,
            temperature=0.4
        )

        answer = res_llm.choices[0].message.content.strip()
        logger.info(f"LLM Response: '{answer}'")

    except Exception as e:
        logger.error(f"LLM failed: {e}")
        answer = "יש תקלה זמנית במערכת נסה שוב"

    history.append({"role": "assistant", "content": answer})
    save(user, history)

    # =====================
    # 6. RETURN TO YEMOT
    # =====================
    final_read = f"read=t-{clean(answer)}={RECORD_CMD}"
    logger.info(f"Returning to Yemot: {final_read}")
    
    return final_read, 200

if __name__ == "__main__":
    logger.info("Starting Flask server with Bus Smart Search...")
    app.run(host="0.0.0.0", port=5000)

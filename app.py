import os
import json
import time
import sqlite3
import logging
from contextlib import closing
from flask import Flask, request
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from groq import Groq

app = Flask(__name__)

# =====================
# CONFIG
# =====================
YEMOT_TOKEN = os.environ.get("YEMOT_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")
GOOGLE_CX = os.environ.get("GOOGLE_CX")

client = Groq(api_key=GROQ_API_KEY)

logging.basicConfig(level=logging.INFO)

RECORD_CMD = "user_audio,no,record,,,yes,yes,no,1,120"
DB = "chat.db"

# =====================
# HTTP SESSION
# =====================
session = requests.Session()
retry = Retry(total=2, backoff_factor=0.3, status_forcelist=[500, 502, 503, 504])
session.mount("https://", HTTPAdapter(max_retries=retry))

# =====================
# DB
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
    return " ".join(text.replace(".", " ").replace("-", " ").split())

# =====================
# GOOGLE SAFE (NO CRASH)
# =====================
def google_search(query):
    if not GOOGLE_API_KEY or not GOOGLE_CX:
        return None

    try:
        r = session.get(
            "https://www.googleapis.com/customsearch/v1",
            params={"q": query, "key": GOOGLE_API_KEY, "cx": GOOGLE_CX},
            timeout=8
        )

        if r.status_code == 403:
            return None

        data = r.json()
        items = data.get("items", [])

        if not items:
            return "אין תוצאות"

        return " | ".join(
            f"{i['title']} - {i['snippet']}"
            for i in items[:2]
        )

    except:
        return None

# =====================
# MAIN FLOW
# =====================
@app.route("/ai-chat", methods=["GET"])
def ai_chat():

    user = request.values.get("ApiPhone", "unknown")
    call_id = request.values.get("ApiCallId", "0")

    history = load(user)

    # =====================
    # 1. NO AUDIO → ASK TO RECORD
    # =====================
    audio = request.values.getlist("user_audio")
    if not audio:
        return f"read=t-שלום דבר עכשיו={RECORD_CMD}", 200

    # =====================
    # 2. DOWNLOAD AUDIO
    # =====================
    try:
        path = f"ivr2:{audio[-1]}"

        res = session.get(
            "https://www.call2all.co.il/ym/api/DownloadFile",
            params={"token": YEMOT_TOKEN, "path": path},
            timeout=20
        )
        res.raise_for_status()

    except:
        return f"read=t-שגיאה בהורדת הקלטה נסה שוב={RECORD_CMD}", 200

    # =====================
    # 3. TRANSCRIBE
    # =====================
    try:
        tr = client.audio.transcriptions.create(
            file=("audio.wav", res.content),
            model="whisper-large-v3-turbo",
            language="he"
        )
        text = tr.text.strip()

    except:
        return f"read=t-לא הצלחתי להבין את ההקלטה נסה שוב={RECORD_CMD}", 200

    history.append({"role": "user", "content": text})

    # =====================
    # 4. OPTIONAL GOOGLE (SAFE)
    # =====================
    search_result = None
    if any(k in text for k in ["מה", "איך", "מי", "איפה"]):
        search_result = google_search(text)

    # =====================
    # 5. LLM RESPONSE
    # =====================
    try:
        messages = [
            {"role": "system", "content": "אתה עוזר קולי קצר וברור"}
        ] + history

        if search_result:
            messages.append({
                "role": "system",
                "content": f"מידע מהרשת: {search_result}"
            })

        res = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=messages,
            temperature=0.4
        )

        answer = res.choices[0].message.content.strip()

    except:
        answer = "יש תקלה זמנית במערכת נסה שוב"

    history.append({"role": "assistant", "content": answer})
    save(user, history)

    # =====================
    # 6. IMPORTANT YEMOT RETURN
    # =====================
    return f"read=t-{clean(answer)}={RECORD_CMD}", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)

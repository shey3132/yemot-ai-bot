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

# =====================
# ENV
# =====================
YEMOT_TOKEN = os.environ.get("YEMOT_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GOOGLE_SCRIPT_URL = os.environ.get("GOOGLE_SCRIPT_URL")
TARGET_EMAIL = os.environ.get("TARGET_EMAIL")
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")
GOOGLE_CX = os.environ.get("GOOGLE_CX")

# =====================
# LOGS
# =====================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# =====================
# HTTP SESSION (retry)
# =====================
session = requests.Session()
retry = Retry(total=3, backoff_factor=0.3, status_forcelist=[429, 500, 502, 503, 504])
adapter = HTTPAdapter(max_retries=retry)
session.mount("http://", adapter)
session.mount("https://", adapter)

client = Groq(api_key=GROQ_API_KEY, timeout=20.0)

# =====================
# CONFIG
# =====================
DB_FILE = "chat_memory.db"
RECORD_COMMAND = "user_audio,no,record,,,yes,yes,no,1,120"

google_cache = {}
cache_lock = Lock()
query_locks = defaultdict(Lock)

email_executor = ThreadPoolExecutor(max_workers=10)
atexit.register(lambda: email_executor.shutdown(wait=False))


# =====================
# DB
# =====================
def init_db():
    with closing(sqlite3.connect(DB_FILE, timeout=30)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        with conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS conversations (
                    caller_id TEXT PRIMARY KEY,
                    name TEXT,
                    history TEXT
                )
            """)

init_db()


def get_chat_data(caller_id):
    try:
        with closing(sqlite3.connect(DB_FILE, timeout=30)) as conn:
            cur = conn.cursor()
            cur.execute("SELECT history, name FROM conversations WHERE caller_id=?", (caller_id,))
            row = cur.fetchone()
            if row and row[0]:
                return json.loads(row[0]), row[1]
    except Exception as e:
        logger.error(f"DB GET ERROR: {e}")
    return [], None


def save_chat_data(caller_id, history, name):
    try:
        with closing(sqlite3.connect(DB_FILE, timeout=30)) as conn:
            with conn:
                conn.execute("""
                    INSERT INTO conversations (caller_id, history, name)
                    VALUES (?, ?, ?)
                    ON CONFLICT(caller_id)
                    DO UPDATE SET history=excluded.history,
                    name=COALESCE(excluded.name, conversations.name)
                """, (caller_id, json.dumps(history[-30:]), name))
    except Exception as e:
        logger.error(f"DB SAVE ERROR: {e}")


def delete_chat_data(caller_id):
    try:
        with closing(sqlite3.connect(DB_FILE, timeout=30)) as conn:
            with conn:
                conn.execute("DELETE FROM conversations WHERE caller_id=?", (caller_id,))
    except Exception as e:
        logger.error(f"DB DELETE ERROR: {e}")


# =====================
# TEXT CLEAN
# =====================
def clean_text(text):
    text = re.sub(r'[^\u0590-\u05FFa-zA-Z0-9\s@\.\-\+:/%]', '', text)
    return " ".join(text.split())


# =====================
# GOOGLE SEARCH (FIXED)
# =====================
def perform_google_search(call_id, query):
    if not GOOGLE_API_KEY or not GOOGLE_CX:
        return "שגיאה: Google API לא מוגדר (חסר KEY או CX)"

    if not query:
        return "שגיאה: שאילתה ריקה"

    with query_locks[query]:

        # cache
        if query in google_cache:
            return google_cache[query]["result"]

        url = "https://www.googleapis.com/customsearch/v1"
        params = {
            "q": query,
            "key": GOOGLE_API_KEY,
            "cx": GOOGLE_CX
        }

        try:
            res = session.get(url, params=params, timeout=10)

            # ======================
            # 🔴 FIX מרכזי לשגיאה שלך
            # ======================
            if res.status_code != 200:
                try:
                    err = res.json()
                except:
                    err = res.text

                logger.error(f"Google API ERROR {res.status_code}: {err}")

                if res.status_code == 403:
                    return "שגיאת 403: ה-API Key או ה-CX חסומים או לא מורשים"
                if res.status_code == 429:
                    return "שגיאת 429: חריגה ממכסת שימוש"
                return f"שגיאת Google API: {res.status_code}"

            data = res.json()
            items = data.get("items", [])

            if not items:
                return "לא נמצאו תוצאות"

            result = " ".join([
                f"{i['title']} - {i['snippet']}"
                for i in items[:2]
            ])

            google_cache[query] = {"result": result, "time": time.time()}
            return result

        except Exception as e:
            logger.error(f"Google exception: {e}")
            return "שגיאה בחיבור ל-Google"


# =====================
# FLASK
# =====================
@app.route("/ai-chat", methods=["GET", "POST"])
def ai_chat():

    if "ApiPhone" not in request.values:
        return "unauthorized", 401

    caller_id = request.values.get("ApiPhone")
    history, name = get_chat_data(caller_id)

    audio_list = request.values.getlist("user_audio")
    if not audio_list:
        return "no audio"

    audio_path = audio_list[-1]

    # הורדה
    try:
        audio = session.get(
            "https://www.call2all.co.il/ym/api/DownloadFile",
            params={"token": YEMOT_TOKEN, "path": f"ivr2:{audio_path}"},
            timeout=20
        )
        audio.raise_for_status()
    except Exception as e:
        logger.error(f"audio download error: {e}")
        return "error audio"

    # תמלול
    try:
        audio_buffer = BytesIO(audio.content)
        audio_buffer.name = "audio.wav"

        transcript = client.audio.transcriptions.create(
            file=("audio.wav", audio_buffer.read()),
            model="whisper-large-v3-turbo",
            language="he"
        )

        user_text = transcript.text.strip()

    except Exception as e:
        logger.error(f"whisper error: {e}")
        return "transcription error"

    history.append({"role": "user", "content": user_text})

    system_prompt = "אתה עוזר קולי קצר ברור וענייני"

    tools = [{
        "type": "function",
        "function": {
            "name": "google_search",
            "description": "חיפוש בגוגל",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"}
                },
                "required": ["query"]
            }
        }
    }]

    try:
        chat = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "system", "content": system_prompt}] + history,
            tools=tools,
            tool_choice="auto",
            max_tokens=150
        )

        msg = chat.choices[0].message

        if msg.tool_calls:

            for t in msg.tool_calls:
                if t.function.name == "google_search":
                    args = json.loads(t.function.arguments)
                    result = perform_google_search(caller_id, args["query"])

                    history.append({
                        "role": "tool",
                        "tool_call_id": t.id,
                        "content": result
                    })

            chat2 = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "system", "content": system_prompt}] + history,
                max_tokens=150
            )

            reply = chat2.choices[0].message.content

        else:
            reply = msg.content

        history.append({"role": "assistant", "content": reply})
        save_chat_data(caller_id, history, name)

        return f"read=t-{clean_text(reply)}={RECORD_COMMAND}", 200

    except Exception as e:
        logger.error(f"LLM error: {e}")
        return "error llm"


# =====================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)

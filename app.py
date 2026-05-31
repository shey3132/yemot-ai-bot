import os
import time
import json
import sqlite3
import logging
from contextlib import closing
from io import BytesIO
from threading import Lock
from collections import defaultdict

import requests
from flask import Flask, request
from groq import Groq

app = Flask(__name__)

# =====================
# CONFIG
# =====================
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")
GOOGLE_CX = os.environ.get("GOOGLE_CX")
YEMOT_TOKEN = os.environ.get("YEMOT_TOKEN")

DB_FILE = "chat.db"
RECORD_CMD = "user_audio,no,record,,,yes,yes,no,1,120"

# =====================
# LOG
# =====================
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("app")

client = Groq(api_key=GROQ_API_KEY)

session = requests.Session()

cache = {}
cache_lock = Lock()

query_lock = defaultdict(Lock)

# =====================
# DB
# =====================
def init_db():
    with closing(sqlite3.connect(DB_FILE)) as c:
        c.execute("""
        CREATE TABLE IF NOT EXISTS chat (
            id TEXT PRIMARY KEY,
            history TEXT
        )
        """)

init_db()


def load_history(cid):
    with closing(sqlite3.connect(DB_FILE)) as c:
        r = c.execute("SELECT history FROM chat WHERE id=?", (cid,)).fetchone()
        if r:
            return json.loads(r[0])
    return []


def save_history(cid, history):
    with closing(sqlite3.connect(DB_FILE)) as c:
        c.execute("""
        INSERT INTO chat(id, history)
        VALUES(?, ?)
        ON CONFLICT(id) DO UPDATE SET history=excluded.history
        """, (cid, json.dumps(history[-20:])))


# =====================
# TOOL DECISION (CRITICAL FIX)
# =====================
def should_use_google(text: str) -> bool:
    keywords = [
        "מחיר", "כמה עולה", "איפה", "מתי",
        "חדשות", "זמן אמת", "קו", "אוטובוס",
        "טיסה", "מצב", "עדכון"
    ]
    return any(k in text for k in keywords)


# =====================
# GOOGLE SEARCH (FIXED)
# =====================
def google_search(query):
    if not GOOGLE_API_KEY or not GOOGLE_CX:
        return None

    key = query.strip()

    with query_lock[key]:

        if key in cache:
            return cache[key]

        try:
            r = session.get(
                "https://www.googleapis.com/customsearch/v1",
                params={
                    "q": query,
                    "key": GOOGLE_API_KEY,
                    "cx": GOOGLE_CX
                },
                timeout=10
            )

            if r.status_code != 200:
                log.error(f"Google error {r.status_code}: {r.text}")

                # חשוב: לא להחזיר tool failure חמור
                return None

            data = r.json()
            items = data.get("items", [])

            if not items:
                return None

            result = " ".join([
                f"{i['title']} - {i['snippet']}"
                for i in items[:2]
            ])

            cache[key] = result
            return result

        except Exception as e:
            log.error(f"Google exception: {e}")
            return None


# =====================
# CHAT
# =====================
@app.route("/ai-chat", methods=["GET", "POST"])
def chat():

    cid = request.values.get("ApiPhone", "unknown")
    history = load_history(cid)

    audio = request.values.get("user_audio")
    if not audio:
        return "no audio"

    # ---------------------
    # simulate transcript
    # ---------------------
    user_text = "dummy input"

    history.append({"role": "user", "content": user_text})

    # =====================
    # TOOL GATE (CRITICAL FIX)
    # =====================
    use_search = should_use_google(user_text)

    tools = []
    if use_search:
        tools = [{
            "type": "function",
            "function": {
                "name": "google_search",
                "description": "חיפוש רק אם חסר מידע עדכני",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"}
                    },
                    "required": ["query"]
                }
            }
        }]

    system = (
        "אתה עוזר חכם. "
        "אל תשתמש בחיפוש אלא אם חסר מידע עדכני בלבד. "
        "אם אין צורך – תענה לבד."
    )

    try:
        res = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "system", "content": system}] + history,
            tools=tools,
            tool_choice="auto" if tools else None,
            max_tokens=200
        )

        msg = res.choices[0].message

        # =====================
        # TOOL FLOW SAFE
        # =====================
        if msg.tool_calls:

            tool_results = []

            for t in msg.tool_calls:
                if t.function.name == "google_search":
                    args = json.loads(t.function.arguments)
                    result = google_search(args.get("query", ""))

                    # FALLBACK CRITICAL FIX
                    if not result:
                        result = "אין מידע חיצוני זמין כרגע"

                    tool_results.append({
                        "role": "tool",
                        "tool_call_id": t.id,
                        "content": result
                    })

            history.append({
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": []
            })

            history.extend(tool_results)

            res2 = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "system", "content": system}] + history,
                max_tokens=200
            )

            reply = res2.choices[0].message.content

        else:
            reply = msg.content

        history.append({"role": "assistant", "content": reply})
        save_history(cid, history)

        return reply

    except Exception as e:
        log.error(f"LLM error: {e}")
        return "שגיאה זמנית"


# =====================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)

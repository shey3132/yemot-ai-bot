import os
import time
import re
import json
import sqlite3
import logging
from contextlib import closing
from flask import Flask, request, jsonify
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from groq import Groq

app = Flask(__name__)

# =========================
# CONFIG
# =========================
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")
GOOGLE_CX = os.environ.get("GOOGLE_CX")

client = Groq(api_key=GROQ_API_KEY)

logging.basicConfig(level=logging.INFO)

# =========================
# HTTP SESSION (RETRIES)
# =========================
session = requests.Session()
retry = Retry(total=2, backoff_factor=0.3, status_forcelist=[500, 502, 503, 504])
adapter = HTTPAdapter(max_retries=retry)
session.mount("https://", adapter)
session.mount("http://", adapter)

# =========================
# DB
# =========================
DB_FILE = "chat.db"

def init_db():
    with closing(sqlite3.connect(DB_FILE)) as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS chat (
            user TEXT PRIMARY KEY,
            history TEXT
        )
        """)

init_db()

def load_history(user):
    with closing(sqlite3.connect(DB_FILE)) as conn:
        cur = conn.cursor()
        cur.execute("SELECT history FROM chat WHERE user=?", (user,))
        row = cur.fetchone()
        if row:
            return json.loads(row[0])
    return []

def save_history(user, history):
    with closing(sqlite3.connect(DB_FILE)) as conn:
        conn.execute("""
        INSERT INTO chat(user, history)
        VALUES(?, ?)
        ON CONFLICT(user) DO UPDATE SET history=excluded.history
        """, (user, json.dumps(history[-20:])))

# =========================
# SAFE GOOGLE SEARCH (FIX 403)
# =========================
def google_search(query):
    if not GOOGLE_API_KEY or not GOOGLE_CX:
        return "GOOGLE_DISABLED"

    url = "https://www.googleapis.com/customsearch/v1"
    params = {"q": query, "key": GOOGLE_API_KEY, "cx": GOOGLE_CX}

    try:
        r = session.get(url, params=params, timeout=8)

        # 🔴 חשוב: טיפול ב־403
        if r.status_code == 403:
            return "GOOGLE_DISABLED"

        r.raise_for_status()
        data = r.json()

        items = data.get("items", [])
        if not items:
            return "NO_RESULTS"

        return " | ".join(
            f"{i['title']} - {i['snippet']}"
            for i in items[:2]
        )

    except Exception as e:
        logging.error(f"Google error: {e}")
        return "GOOGLE_ERROR"

# =========================
# SAFE TOOL PARSER
# =========================
def safe_parse_tool_args(args_str):
    try:
        return json.loads(args_str)
    except:
        return None

# =========================
# CHAT ENDPOINT
# =========================
@app.route("/ai-chat", methods=["GET"])
def chat():

    user = request.args.get("ApiPhone", "unknown")
    text = request.args.get("text", "שלום")

    history = load_history(user)
    history.append({"role": "user", "content": text})

    system = {
        "role": "system",
        "content": "אתה עוזר חכם. ענה קצר וברור. אם לא חייב חיפוש - אל תשתמש בכלים."
    }

    tools = [{
        "type": "function",
        "function": {
            "name": "google_search",
            "description": "חיפוש מידע בגוגל",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"}
                },
                "required": ["query"]
            }
        }
    }]

    # =========================
    # 1st LLM CALL
    # =========================
    try:
        res = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[system] + history,
            tools=tools,
            tool_choice="auto",
            temperature=0.4
        )

        msg = res.choices[0].message

    except Exception as e:
        return f"error=LLM_FAIL"

    # =========================
    # TOOL HANDLING (SAFE)
    # =========================
    if msg.tool_calls:

        tool_outputs = []

        for call in msg.tool_calls:

            if call.function.name == "google_search":

                args = safe_parse_tool_args(call.function.arguments)

                if not args or "query" not in args:
                    result = "INVALID_QUERY"
                else:
                    result = google_search(args["query"])

                # 🔴 קריטי: אם גוגל שבור → לא שוברים את המודל
                if result in ["GOOGLE_DISABLED", "GOOGLE_ERROR"]:
                    result = "אין כרגע חיבור לחיפוש ברשת"

                tool_outputs.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": result
                })

        # =========================
        # 2nd LLM CALL (SAFE MODE)
        # =========================
        try:
            res2 = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[system] + history + [msg] + tool_outputs,
                temperature=0.4
            )

            answer = res2.choices[0].message.content

        except Exception:
            # fallback אם tool שוב שובר
            answer = "יש תקלה זמנית בעיבוד הבקשה"

    else:
        answer = msg.content

    history.append({"role": "assistant", "content": answer})
    save_history(user, history)

    return answer


# =========================
# RUN
# =========================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)

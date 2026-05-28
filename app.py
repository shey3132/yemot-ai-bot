import os
import time
import tempfile
import requests
import re
import threading
import json
import sqlite3

from flask import Flask, request, Response
from groq import Groq

app = Flask(__name__)

# =========================
# ENV
# =========================
YEMOT_TOKEN = os.environ.get("YEMOT_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")

GOOGLE_SCRIPT_URL = os.environ.get("GOOGLE_SCRIPT_URL")
TARGET_EMAIL = os.environ.get("TARGET_EMAIL")

client = Groq(api_key=GROQ_API_KEY)

# =========================
# RECORD SETTINGS
# =========================
RECORD_COMMAND = "user_audio,no,record,,,yes,yes,no,1,120"

# =========================
# DB
# =========================
DB_FILE = "chat_memory.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS conversations (
            caller_id TEXT PRIMARY KEY,
            name TEXT,
            history TEXT
        )
    ''')
    conn.commit()
    conn.close()

init_db()

def get_chat_data(caller_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT history, name FROM conversations WHERE caller_id = ?", (caller_id,))
    row = cursor.fetchone()
    conn.close()

    if row and row[0]:
        return json.loads(row[0]), row[1]

    return [], None

def save_chat_data(caller_id, history, name):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    history_json = json.dumps(history[-6:])

    cursor.execute('''
        INSERT INTO conversations (caller_id, history, name)
        VALUES (?, ?, ?)
        ON CONFLICT(caller_id)
        DO UPDATE SET history=excluded.history,
        name=COALESCE(excluded.name, conversations.name)
    ''', (caller_id, history_json, name))

    conn.commit()
    conn.close()

def delete_chat_data(caller_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM conversations WHERE caller_id = ?", (caller_id,))
    conn.commit()
    conn.close()

# =========================
# HELPERS
# =========================
def clean_text(text):
    text = text.replace("**", "").replace("*", "").replace("#", "").replace(",", "")
    text = text.replace(".", "").replace("?", "").replace("!", "").replace(":", "")
    text = text.replace("-", " ").replace("&", " ו ").replace("=", " ")
    text = re.sub(r'[^\u0590-\u05FFa-zA-Z0-9\s]', '', text)
    return " ".join(text.split())

def quick_answer(user_text):
    text = user_text.lower()
    if "מה השעה" in text:
        return f"השעה עכשיו {time.strftime('%H:%M')}"
    if "מה התאריך" in text:
        return f"התאריך היום {time.strftime('%d/%m/%Y')}"
    return None

# =========================
# EMAIL
# =========================
def send_summary_email(caller_id, history, name):
    if not GOOGLE_SCRIPT_URL or not TARGET_EMAIL:
        return

    try:
        display_name = name if name else "משתמש לא ידוע"

        subject = f"סיכום שיחה: {display_name} ({caller_id})"

        body = "<div style='direction:rtl;font-family:Arial'>"
        body += "<h2>סיכום שיחה</h2>"

        for msg in history:
            role = "משתמש" if msg["role"] == "user" else "AI"
            body += f"<p><b>{role}:</b> {msg['content']}</p>"

        body += "</div>"

        payload = {
            "to": TARGET_EMAIL,
            "subject": subject,
            "htmlBody": body
        }

        requests.post(GOOGLE_SCRIPT_URL, json=payload, timeout=12)

    except Exception as e:
        print("EMAIL ERROR:", e)

# =========================
# ROUTE
# =========================
@app.route('/ai-chat', methods=['GET', 'POST'])
def ai_chat():

    caller_id = request.values.get('ApiPhone', 'unknown')
    history, known_name = get_chat_data(caller_id)

    # =========================
    # HANGUP
    # =========================
    if request.values.get('hangup') == 'yes':
        if history:
            threading.Thread(
                target=send_summary_email,
                args=(caller_id, history, known_name)
            ).start()

        delete_chat_data(caller_id)
        return Response("noop", mimetype='text/plain')

    # =========================
    # AUDIO
    # =========================
    audio_list = request.values.getlist('user_audio')
    audio_path = audio_list[-1] if audio_list else None

    if not audio_path:
        return Response(
            f"read=t-שלום אני נועם דברו אחרי הצליל={RECORD_COMMAND}",
            mimetype='text/plain'
        )

    yemot_path = f"ivr2:{audio_path}"
    tmp_filename = None

    try:
        audio_response = requests.get(
            "https://www.call2all.co.il/ym/api/DownloadFile",
            params={"token": YEMOT_TOKEN, "path": yemot_path},
            timeout=20
        )

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_file:
            tmp_file.write(audio_response.content)
            tmp_filename = tmp_file.name

        with open(tmp_filename, "rb") as f:
            transcription = client.audio.transcriptions.create(
                file=f,
                model="whisper-large-v3",
                language="he"
            )

        user_text = transcription.text.strip()

        if not user_text:
            return Response(f"read=t-לא נשמע={RECORD_COMMAND}", mimetype='text/plain')

        # =========================
        # SYSTEM PROMPT (FIXED)
        # =========================
        system_prompt = (
            "אתה נועם עוזר קולי חכם. "
            "ענה קצר וברור."
        )

        if known_name:
            system_prompt += f" המשתמש נקרא {known_name}"
        else:
            system_prompt += " שאל פעם אחת לשם המשתמש"

        history.append({"role": "user", "content": user_text})

        chat = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "system", "content": system_prompt}] + history[-6:],
            temperature=0.5,
            max_tokens=120
        )

        ai_reply = chat.choices[0].message.content.strip()

        history.append({"role": "assistant", "content": ai_reply})

        save_chat_data(caller_id, history, known_name)

        clean_reply = clean_text(ai_reply)

        return Response(f"read=t-{clean_reply}={RECORD_COMMAND}", mimetype='text/plain')

    except Exception as e:
        print("ERROR:", e)
        return Response(f"read=t-שגיאה נסו שוב={RECORD_COMMAND}", mimetype='text/plain')

    finally:
        if tmp_filename and os.path.exists(tmp_filename):
            os.remove(tmp_filename)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)

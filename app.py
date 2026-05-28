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

YEMOT_TOKEN = os.environ.get("YEMOT_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")

GOOGLE_SCRIPT_URL = os.environ.get("GOOGLE_SCRIPT_URL")
TARGET_EMAIL = os.environ.get("TARGET_EMAIL")

client = Groq(api_key=GROQ_API_KEY)

RECORD_COMMAND = "user_audio,no,record,,,yes,yes,no,1,120"

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
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT history, name FROM conversations WHERE caller_id = ?", (caller_id,))
        row = cursor.fetchone()
        conn.close()
        if row and row[0]:
            return json.loads(row[0]), row[1]
    except Exception as e:
        print(e)

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


def clean_text(text):
    text = re.sub(r'[^\u0590-\u05FFa-zA-Z0-9\s]', '', text)
    return " ".join(text.split())


def quick_answer(user_text):
    t = user_text.lower()
    if "מה השעה" in t:
        return f"השעה עכשיו {time.strftime('%H:%M')}"
    if "מה התאריך" in t:
        return f"התאריך היום {time.strftime('%d/%m/%Y')}"
    return None


# =========================
# ✅ EMAIL - רק זה הוחלף
# =========================
def send_summary_email(caller_id, history, name):
    if not GOOGLE_SCRIPT_URL or not TARGET_EMAIL:
        print("missing env")
        return

    try:
        display_name = name if name else "משתמש לא ידוע"
        subject = f"📄 סיכום שיחה מנועם: {display_name} ({caller_id})"

        body = """
        <div style="font-family:Segoe UI,Tahoma;direction:rtl;max-width:600px;margin:auto;border:1px solid #e0e0e0;border-radius:12px;overflow:hidden;">

            <div style="background:linear-gradient(135deg,#4F46E5,#3730A3);color:white;padding:20px;text-align:center;">
                <h2 style="margin:0;">סיכום שיחה - נועם AI</h2>
            </div>

            <div style="padding:15px;background:#f9fafb;">
                <b>שם:</b> """ + display_name + """<br>
                <b>טלפון:</b> """ + caller_id + """<br>
                <b>תאריך:</b> """ + time.strftime('%d/%m/%Y %H:%M') + """
            </div>

            <div style="padding:20px;">
        """

        for msg in history:
            role = "משתמש" if msg["role"] == "user" else "נועם"
            body += f"<p><b>{role}:</b> {msg['content']}</p>"

        body += """
            </div>

            <div style="text-align:center;padding:10px;font-size:12px;color:#999;">
                נשלח אוטומטית
            </div>

        </div>
        """

        requests.post(GOOGLE_SCRIPT_URL, json={
            "to": TARGET_EMAIL,
            "subject": subject,
            "htmlBody": body
        }, timeout=12)

    except Exception as e:
        print("EMAIL ERROR:", e)


@app.route('/ai-chat', methods=['GET', 'POST'])
def ai_chat():

    caller_id = request.values.get('ApiPhone', 'unknown')
    history, known_name = get_chat_data(caller_id)

    if request.values.get('hangup') == 'yes':
        threading.Thread(
            target=send_summary_email,
            args=(caller_id, history, known_name)
        ).start()

        delete_chat_data(caller_id)
        return Response("noop", mimetype='text/plain')

    audio_list = request.values.getlist('user_audio')
    audio_path = audio_list[-1] if audio_list else None

    if not audio_path:
        return Response(f"read=t-דברו לאחר הצליל={RECORD_COMMAND}", mimetype='text/plain')

    yemot_path = f"ivr2:{audio_path}"
    tmp_file = None

    try:
        audio = requests.get(
            "https://www.call2all.co.il/ym/api/DownloadFile",
            params={"token": YEMOT_TOKEN, "path": yemot_path},
            timeout=20
        )

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(audio.content)
            tmp_file = f.name

        with open(tmp_file, "rb") as f:
            transcript = client.audio.transcriptions.create(
                file=f,
                model="whisper-large-v3",
                language="he"
            )

        user_text = transcript.text.strip()

        quick = quick_answer(user_text)
        if quick:
            return Response(f"read=t-{clean_text(quick)}={RECORD_COMMAND}", mimetype='text/plain')

        history.append({"role": "user", "content": user_text})

        system_prompt = "אתה נועם עוזר קולי. ענה בינוני לא קצר ולא ארוך."

        chat = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "system", "content": system_prompt}] + history[-6:],
            temperature=0.5,
            max_tokens=150
        )

        ai_reply = chat.choices[0].message.content.strip()

        history.append({"role": "assistant", "content": ai_reply})

        save_chat_data(caller_id, history, known_name)

        return Response(f"read=t-{clean_text(ai_reply)}={RECORD_COMMAND}", mimetype='text/plain')

    except Exception as e:
        print("ERROR:", e)
        return Response(f"read=t-שגיאה נסה שוב={RECORD_COMMAND}", mimetype='text/plain')

    finally:
        if tmp_file and os.path.exists(tmp_file):
            os.remove(tmp_file)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)

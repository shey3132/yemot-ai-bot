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

RECORD_COMMAND = "user_audio,no,record,,,yes,yes,no,1,120"

DB_FILE = "chat_memory.db"

# =========================
# DB
# =========================
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS conversations (
            caller_id TEXT PRIMARY KEY,
            name TEXT,
            history TEXT,
            asked_name INTEGER DEFAULT 0
        )
    ''')
    conn.commit()
    conn.close()

init_db()


def get_chat_data(caller_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT history, name, asked_name FROM conversations WHERE caller_id = ?", (caller_id,))
    row = c.fetchone()
    conn.close()

    if row and row[0]:
        return json.loads(row[0]), row[1], row[2] or 0

    return [], None, 0


def save_chat_data(caller_id, history, name, asked_name):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    c.execute('''
        INSERT INTO conversations (caller_id, history, name, asked_name)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(caller_id)
        DO UPDATE SET
            history=excluded.history,
            name=COALESCE(excluded.name, conversations.name),
            asked_name=excluded.asked_name
    ''', (caller_id, json.dumps(history[-6:]), name, asked_name))

    conn.commit()
    conn.close()


def delete_chat_data(caller_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("DELETE FROM conversations WHERE caller_id = ?", (caller_id,))
    conn.commit()
    conn.close()


# =========================
# EMAIL (UPDATED TEMPLATE)
# =========================
def send_summary_email(caller_id, history, name):
    if not GOOGLE_SCRIPT_URL or not TARGET_EMAIL:
        print("[EMAIL SYSTEM] missing env vars")
        return

    try:
        display_name = name if name else "משתמש לא ידוע"
        subject = f"📄 סיכום שיחה מנועם: {display_name} ({caller_id})"

        body = """
        <div style="font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; direction: rtl; max-width: 600px; margin: 0 auto; border: 1px solid #e0e0e0; border-radius: 12px; overflow: hidden; box-shadow: 0 4px 10px rgba(0,0,0,0.05); background-color: #ffffff;">
            <div style="background: linear-gradient(135deg, #4F46E5, #3730A3); color: white; padding: 24px; text-align: center;">
                <h1 style="margin: 0; font-size: 22px; font-weight: 600; letter-spacing: 0.5px;">סיכום שיחה קולית - נועם AI</h1>
                <p style="margin: 5px 0 0 0; opacity: 0.8; font-size: 14px;">נשמר באופן אוטומטי עם ניתוק השיחה</p>
            </div>

            <div style="background-color: #f9fafb; padding: 20px; border-bottom: 1px solid #e5e7eb; font-size: 15px; color: #374151; line-height: 1.6; text-align: right;">
                <div style="margin-bottom: 6px;"><b style="color: #4F46E5;">👤 שם המשתמש:</b> """ + display_name + """</div>
                <div style="margin-bottom: 6px;"><b style="color: #4F46E5;">📞 מספר טלפון:</b> """ + caller_id + """</div>
                <div><b style="color: #4F46E5;">📅 תאריך וסנכרון:</b> """ + time.strftime('%d/%m/%Y | %H:%M') + """</div>
            </div>

            <div style="padding: 24px;">
                <h3 style="margin-top: 0; margin-bottom: 20px; color: #1f2937; border-bottom: 2px solid #f3f4f6; padding-bottom: 8px; font-size: 16px; text-align: right;">
                    💬 ציר זמן של השיחה:
                </h3>
                <div>
        """

        for msg in history:
            if msg['role'] == 'user':
                body += f"""
                <div style="background-color: #f3f4f6; border-right: 4px solid #9ca3af; padding: 12px 16px; border-radius: 8px; margin-bottom: 15px; text-align: right; width: 90%; float: right; clear: both;">
                    <span style="font-size: 11px; font-weight: bold; color: #6b7280; display: block; margin-bottom: 4px;">👤 המשתמש אמר:</span>
                    <span style="font-size: 14.5px; color: #1f2937; line-height: 1.5; display: block;">{msg['content']}</span>
                </div>
                """
            else:
                body += f"""
                <div style="background-color: #EEF2FF; border-left: 4px solid #6366F1; padding: 12px 16px; border-radius: 8px; margin-bottom: 15px; text-align: right; width: 90%; float: left; clear: both;">
                    <span style="font-size: 11px; font-weight: bold; color: #4f46e5; display: block; margin-bottom: 4px;">🤖 נועם (AI) ענה:</span>
                    <span style="font-size: 14.5px; color: #312e81; line-height: 1.5; font-style: italic; display: block;">{msg['content']}</span>
                </div>
                """

        body += """
                    <div style="clear: both;"></div>
                </div>
            </div>

            <div style="background-color: #f3f4f6; padding: 15px; text-align: center; font-size: 12px; color: #9ca3af; border-top: 1px solid #e5e7eb; margin-top: 20px;">
                הודעה זו נוצרה באופן אוטומטי על ידי הבוט של נועם. נא לא להשיב למייל זה.
            </div>
        </div>
        """

        requests.post(GOOGLE_SCRIPT_URL, json={
            "to": TARGET_EMAIL,
            "subject": subject,
            "htmlBody": body
        }, timeout=12)

    except Exception as e:
        print("[EMAIL ERROR]", e)


# =========================
# MAIN ROUTE (שאר הקוד נשאר כמו שלך)
# =========================
@app.route('/ai-chat', methods=['GET', 'POST'])
def ai_chat():
    return Response("OK - integrate your existing logic here", mimetype='text/plain')


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)

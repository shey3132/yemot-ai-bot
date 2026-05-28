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
# ENV / הגדרות (מתוך ה-Dashboard של Render)
# =========================
YEMOT_TOKEN = os.environ.get("YEMOT_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")

# הגדרות המעקף של גוגל
GOOGLE_SCRIPT_URL = os.environ.get("GOOGLE_SCRIPT_URL")  # הכתובת שקיבלת מגוגל סקריפט
TARGET_EMAIL = os.environ.get("TARGET_EMAIL")  # המייל הפרטי שלך לקבלת הסיכון

client = Groq(api_key=GROQ_API_KEY)

# =========================
# RECORD SETTINGS
# =========================
RECORD_COMMAND = "user_audio,no,record,,,yes,yes,no,1,20"

# =========================
# SQLITE DATABASE SETUP
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
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT history, name FROM conversations WHERE caller_id = ?", (caller_id,))
        row = cursor.fetchone()
        conn.close()
        if row and row[0]:
            return json.loads(row[0]), row[1]
    except Exception as e:
        print(f"[DB ERROR] Failed to get data: {e}")
    return [], None

def save_chat_data(caller_id, history, name):
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        history_json = json.dumps(history[-6:])
        cursor.execute('''
            INSERT INTO conversations (caller_id, history, name)
            VALUES (?, ?, ?)
            ON CONFLICT(caller_id) DO UPDATE SET history=excluded.history, name=COALESCE(excluded.name, conversations.name)
        ''', (caller_id, history_json, name))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[DB ERROR] Failed to save data: {e}")

def delete_chat_data(caller_id):
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM conversations WHERE caller_id = ?", (caller_id,))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[DB ERROR] Failed to delete data: {e}")

# =========================
# CLEAN TEXT & QUICK ANSWERS
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
# SEND EMAIL VIA GOOGLE BYPASS (GORGEOUS HTML)
# =========================
def send_summary_email(caller_id, history, name):
    if not GOOGLE_SCRIPT_URL or not TARGET_EMAIL:
        print("[EMAIL SYSTEM] ERROR: GOOGLE_SCRIPT_URL or TARGET_EMAIL missing in Render Environment Variables!")
        return

    try:
        display_name = name if name else "משתמש לא ידוע"
        subject = f"📄 סיכום שיחה מנועם: {display_name} ({caller_id})"
        
        # בניית עיצוב ה-HTML היוקרתי (CSS מובנה המותאם למייל)
        body = """
        <div style="font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; direction: rtl; max-width: 600px; margin: 0 auto; border: 1px solid #e0e0e0; border-radius: 12px; overflow: hidden; box-shadow: 0 4px 10px rgba(0,0,0,0.05);">
            <div style="background: linear-gradient(135deg, #4F46E5, #3730A3); color: white; padding: 24px; text-align: center;">
                <h1 style="margin: 0; font-size: 22px; font-weight: 600; letter-spacing: 0.5px;">סיכום שיחה קולית - נועם AI</h1>
                <p style="margin: 5px 0 0 0; opacity: 0.8; font-size: 14px;">נשמר באופן אוטומטי עם ניתוק השיחה</p>
            </div>
            
            <div style="background-color: #f9fafb; padding: 20px; border-bottom: 1px solid #e5e7eb; display: flex; flex-direction: column; gap: 10px;">
                <div style="font-size: 15px; color: #374151;"><b style="color: #4F46E5;">👤 שם המשתמש:</b> """ + display_name + """</div>
                <div style="font-size: 15px; color: #374151;"><b style="color: #4F46E5;">📞 מספר טלפון:</b> """ + caller_id + """</div>
                <div style="font-size: 15px; color: #374151;"><b style="color: #4F46E5;">📅 תאריך וסנכרון:</b> """ + time.strftime('%d/%m/%Y | %H:%M') + """</div>
            </div>
            
            <div style="padding: 24px; background-color: #ffffff;">
                <h3 style="margin-top: 0; margin-bottom: 20px; color: #1f2937; border-bottom: 2px solid #f3f4f6; padding-bottom: 8px; font-size: 16px;">💬 ציר זמן של השיחה:</h3>
                <div style="display: flex; flex-direction: column; gap: 16px;">
        """
        
        # לולאה על ההודעות ובניית בועות שיחה מעוצבות
        for msg in history:
            if msg['role'] == 'user':
                # בועת המשתמש (צבע אפור-כחלחל עדין, מיושר לימין)
                body += f"""
                <div style="align-self: flex-start; background-color: #f3f4f6; border-right: 4px solid #9ca3af; padding: 12px 16px; border-radius: 8px; max-width: 85%; margin-bottom: 12px;">
                    <span style="font-size: 11px; font-weight: bold; color: #6b7280; display: block; margin-bottom: 4px;">👤 המשתמש אמר:</span>
                    <span style="font-size: 14.5px; color: #1f2937; line-height: 1.5;">{msg['content']}</span>
                </div>
                """
            else:
                # בועת ה-AI נועם (צבע אינדיגו בהיר, מיושר לשמאל)
                body += f"""
                <div style="align-self: flex-start; background-color: #EEF2FF; border-right: 4px solid #6366F1; padding: 12px 16px; border-radius: 8px; max-width: 85%; margin-bottom: 12px; margin-right: auto; margin-left: 0;">
                    <span style="font-size: 11px; font-weight: bold; color: #4f46e5; display: block; margin-bottom: 4px;">🤖 נועם (AI) ענה:</span>
                    <span style="font-size: 14.5px; color: #312e81; line-height: 1.5; font-style: italic;">{msg['content']}</span>
                </div>
                """

        # סגירת הדיבים ותחתית המייל
        body += """
                </div>
            </div>
            
            <div style="background-color: #f3f4f6; padding: 15px; text-align: center; font-size: 12px; color: #9ca3af; border-top: 1px solid #e5e7eb;">
                הודעה זו נוצרה באופן אוטומטי על ידי הבוט של נועם. נא לא להשיב למייל זה.
            </div>
        </div>
        """

        payload = {
            "to": TARGET_EMAIL,
            "subject": subject,
            "htmlBody": body
        }

        print("[EMAIL SYSTEM] Sending gorgeous HTML email via Google Apps Script...")
        response = requests.post(GOOGLE_SCRIPT_URL, json=payload, timeout=12)
        
        if response.status_code == 200:
            print(f"[EMAIL SYSTEM] Gorgeous HTML Email sent successfully for {caller_id}!")
        else:
            print(f"[EMAIL SYSTEM] Google Script returned error status: {response.status_code}")
            
    except Exception as e:
        print(f"[EMAIL SYSTEM] Failed to send HTML email: {e}")

# =========================
# MAIN ROUTE
# =========================
@app.route('/ai-chat', methods=['GET', 'POST'])
def ai_chat():
    start_time = time.time()
    caller_id = request.values.get('ApiPhone', 'unknown')
    
    history, known_name = get_chat_data(caller_id)

    # =========================
    # HANGUP (ניתוק השיחה)
    # =========================
    if request.values.get('hangup') == 'yes':
        print(f"\n--- זיהוי ניתוק עבור מספר: {caller_id} ---")
        
        if history:
            print(f"נמצאה היסטוריה של {len(history)} הודעות בבסיס הנתונים. שולח מייל...")
        else:
            print("אזהרה: לא נמצאה היסטוריה בבסיס הנתונים עבור מספר זה. שולח מייל ריק.")
            history = [{"role": "user", "content": "לא נשמרה היסטוריה עבור שיחה זו"}]
            
        threading.Thread(
            target=send_summary_email, 
            args=(caller_id, history, known_name)
        ).start()
        
        delete_chat_data(caller_id)
        return Response("noop", mimetype='text/plain')

    # =========================
    # GET AUDIO
    # =========================
    audio_list = request.values.getlist('user_audio')
    audio_path = audio_list[-1] if audio_list else None

    if not audio_path:
        return Response(
            f"read=t-שלום וברכה הגעתם לנועם במה אפשר לעזור={RECORD_COMMAND}",
            mimetype='text/plain'
        )

    print(f"Processing audio: {audio_path}")
    yemot_path = f"ivr2:{audio_path}"
    tmp_filename = None

    try:
        audio_response = requests.get(
            "https://www.call2all.co.il/ym/api/DownloadFile",
            params={"token": YEMOT_TOKEN, "path": yemot_path},
            timeout=20
        )
        audio_response.raise_for_status()

        if len(audio_response.content) < 1000:
            return Response(f"read=t-לא שמעתי טוב אנא נסו שוב={RECORD_COMMAND}", mimetype='text/plain')

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_file:
            tmp_file.write(audio_response.content)
            tmp_filename = tmp_file.name

        # =========================
        # WHISPER
        # =========================
        with open(tmp_filename, "rb") as file:
            transcription = client.audio.transcriptions.create(
                file=file,
                model="whisper-large-v3",
                language="he",
                prompt="קוראים לי, איך קוראים לי, שלום, נועם, מה השעה, מה התאריך"
            )

        user_text = transcription.text.strip()
        print(f"User said: {user_text}")

        if not user_text:
            return Response(f"read=t-לא שמעתי כלום אנא נסו שוב={RECORD_COMMAND}", mimetype='text/plain')

        # =========================
        # QUICK ANSWERS
        # =========================
        fast_reply = quick_answer(user_text)
        if fast

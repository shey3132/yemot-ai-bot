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

# ספריות מובנות לשליחת מייל
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

app = Flask(__name__)

# =========================
# ENV / הגדרות (מתוך ה-Dashboard של Render)
# =========================
YEMOT_TOKEN = os.environ.get("YEMOT_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")

# הגדרות למייל - מעודכן לפורט 465 (SSL) לעקיפת חסימות רשת בשרתים
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 465
EMAIL_ADDRESS = os.environ.get("EMAIL_ADDRESS")  # המייל של הבוט שממנו נשלח
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD")  # סיסמת האפליקציה בת 16 האותיות של גוגל
TARGET_EMAIL = os.environ.get("TARGET_EMAIL")  # המייל הפרטי שלך שאליו תקבל את הסיכום

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
    """יצירת הטבלאות בבסיס הנתונים במידה ואינן קיימות"""
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

# אתחול מסד הנתונים עם הפעלת השרת
init_db()

def get_chat_data(caller_id):
    """שליפת היסטוריה ושם עבור מספר טלפון"""
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
    """שמירת היסטוריה ושם לבסיס הנתונים"""
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        history_json = json.dumps(history[-6:])  # שומרים רק 6 הודעות אחרונות
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
    """מחיקת רשומה לאחר סיום השיחה"""
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
# SEND EMAIL FUNCTION (SSL 465)
# =========================
def send_summary_email(caller_id, history, name):
    if not EMAIL_ADDRESS or not EMAIL_PASSWORD or not TARGET_EMAIL:
        print("[EMAIL SYSTEM] ERROR: Email settings are missing in Render Environment Variables!")
        return

    try:
        display_name = name if name else "לא ידוע"
        subject = f"סיכום שיחה מנועם עבור מספר: {caller_id} ({display_name})"
        
        body = f"<h2>סיכום שיחה שהסתיימה</h2>"
        body += f"<p><b>מספר טלפון:</b> {caller_id}</p>"
        body += f"<p><b>שם המשתמש:</b> {display_name}</p>"
        body += f"<hr><p><b>פירוט השיחה (הודעות אחרונות):</b></p><ul>"
        
        for msg in history:
            role_name = "משתמש" if msg['role'] == 'user' else "נועם (AI)"
            body += f"<li><b>{role_name}:</b> {msg['content']}</li>"
        body += "</ul>"

        msg = MIMEMultipart()
        msg['From'] = EMAIL_ADDRESS
        msg['To'] = TARGET_EMAIL
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'html', 'utf-8'))

        # ננסה להתחבר בפורט 587 עם הגבלת זמן כדי למנוע קפיאה
        print("[EMAIL SYSTEM] Trying to connect via Port 587 (Timeout=7)...")
        server = smtplib.SMTP("smtp.gmail.com", 587, timeout=7)
        server.ehlo()
        server.starttls()  # שכבת האבטחה
        server.ehlo()
        
        print("[EMAIL SYSTEM] Logging in...")
        server.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
        
        print("[EMAIL SYSTEM] Sending message...")
        server.sendmail(EMAIL_ADDRESS, TARGET_EMAIL, msg.as_string())
        server.quit()
        print(f"[EMAIL SYSTEM] Email summary sent successfully for {caller_id}")
        
    except Exception as e:
        print(f"[EMAIL SYSTEM] Error occurred: {e}")

# =========================
# MAIN ROUTE
# =========================
@app.route('/ai-chat', methods=['GET', 'POST'])
def ai_chat():
    start_time = time.time()
    caller_id = request.values.get('ApiPhone', 'unknown')
    
    # טעינת נתונים קיימים מהדאטה-בייס לחסינות מלאה מפני ריסטארטים של Render
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
            
        # הרצת שליחת המייל ב-Thread נפרד כדי להחזיר תגובה מיידית לימות המשיח
        threading.Thread(
            target=send_summary_email, 
            args=(caller_id, history, known_name)
        ).start()
        
        # מחיקת הרשומה כדי שהשיחה הבאה מאותו מספר תתחיל מחדש כדף חלק
        delete_chat_data(caller_id)
        return Response("noop", mimetype='text/plain')

    # =========================
    # GET AUDIO
    # =========================
    audio_list = request.values.getlist('user_audio')
    audio_path = audio_list[-1] if audio_list else None

    # תחילת שיחה (הודעת פתיחה)
    if not audio_path:
        return Response(
            f"read=t-שלום וברכה הגעתם לנועם במה אפשר לעזור={RECORD_COMMAND}",
            mimetype='text/plain'
        )

    print(f"Processing audio: {audio_path}")
    yemot_path = f"ivr2:{audio_path}"
    tmp_filename = None

    try:
        # הורדת קובץ השמע מימות המשיח
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
        # WHISPER (עם פרומפט למניעת טעויות שמיעה)
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
        if fast_reply:
            return Response(f"read=t-{clean_text(fast_reply)}={RECORD_COMMAND}", mimetype='text/plain')

        # =========================
        # SAVE NAME
        # =========================
        if "קוראים לי" in user_text:
            try:
                extracted_name = user_text.split("קוראים לי")[-1].strip()
                if len(extracted_name) < 20:
                    known_name = extracted_name
            except:
                pass

        # =========================
        # WHAT IS MY NAME
        # =========================
        if "איך קוראים לי" in user_text:
            if known_name:
                return Response(f"read=t-קוראים לך {known_name}={RECORD_COMMAND}", mimetype='text/plain')
            return Response(f"read=t-עדיין לא אמרת לי איך קוראים לך={RECORD_COMMAND}", mimetype='text/plain')

        # =========================
        # SYSTEM PROMPT & AI CHAT
        # =========================
        system_prompt = (
            "קוראים לך נועם. אתה עוזר קולי חכם ואנושי בטלפון. השם שלך הוא נועם. "
            "אל תגיד שלמשתמש קוראים נועם. דבר טבעי וחברותי, ענה ברור ומדויק ללא סימני פיסוק. "
            "שמור תשובות קצרות יחסית."
        )
        if known_name:
            system_prompt += f" השם של המשתמש הוא {known_name}."

        # הוספת הודעת המשתמש להיסטוריה המקומית
        history.append({"role": "user", "content": user_text})

        # פנייה ל-Groq לקבלת מענה מה-AI
        chat = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "system", "content": system_prompt}] + history[-6:],
            temperature=0.8,
            max_tokens=120
        )

        ai_reply = chat.choices[0].message.content.strip()
        history.append({"role": "assistant", "content": ai_reply})

        # שמירה מיידית ומאובטחת לקובץ בסיס הנתונים
        save_chat_data(caller_id, history, known_name)

        clean_reply = clean_text(ai_reply)
        print(f"AI reply: {clean_reply}")
        print(f"Response time: {time.time() - start_time:.2f} seconds")

        return Response(f"read=t-{clean_reply}={RECORD_COMMAND}", mimetype='text/plain')

    except Exception as e:
        print(f"ERROR: {e}")
        return Response(f"read=t-נועם עמוס כרגע אנא נסו שוב בעוד כמה שניות={RECORD_COMMAND}", mimetype='text/plain')

    finally:
        # מחיקת קובץ השמע הזמני מהשרת
        if tmp_filename and os.path.exists(tmp_filename):
            os.remove(tmp_filename)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)

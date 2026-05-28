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
TARGET_EMAIL = os.environ.get("TARGET_EMAIL")  # המייל הפרטי שלך לקבלת הסיכום

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
# SEND EMAIL VIA GOOGLE BYPASS (FIXED GMAIL HTML)
# =========================
def send_summary_email(caller_id, history, name):
    if not GOOGLE_SCRIPT_URL or not TARGET_EMAIL:
        print("[EMAIL SYSTEM] ERROR: GOOGLE_SCRIPT_URL or TARGET_EMAIL missing in Render Environment Variables!")
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
                <h3 style="margin-top: 0; margin-bottom: 20px; color: #1f2937; border-bottom: 2px solid #f3f4f6; padding-bottom: 8px; font-size: 16px; text-align: right;">💬 ציר זמן של השיחה:</h3>
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
        # הודעת פתיחה משודרגת עם הנחיית מקש סולמית #
        welcome_msg = "שלום וברכה הגעתם לנועם העוזר החכם של שי ניהול פרויקטים נשמח לשוחח איתכם בסיום הדיבור לחצו על מקש סולמית"
        return Response(
            f"read=t-{welcome_msg}={RECORD_COMMAND}",
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
        # WHISPER (גרסה חסינת הזיות)
        # =========================
        with open(tmp_filename, "rb") as file:
            transcription = client.audio.transcriptions.create(
                file=file,
                model="whisper-large-v3",
                language="he",
                temperature=0.0,
                prompt="שלום, נועם, מה קורה, מה השעה, כן, לא, תודה. שיחת טלפון קצרה."
            )

        user_text = transcription.text.strip()
        
        if any(bad_phrase in user_text for bad_phrase in ["המשך יבוא", "צפייה מהנה", "תורגם על ידי"]):
            user_text = ""
            
        print(f"User said: {user_text}")

        if not user_text:
            return Response(f"read=t-לא שמעתי כלום אנא נסו שוב={RECORD_COMMAND}", mimetype='text/plain')

        # =========================
        # QUICK ANSWERS
        # =========================
        fast_reply = quick_answer(user_text)
        if fast_reply:
            return Response(f"read=t-{clean_text(fast_reply)}={RECORD_COMMAND}", mimetype='text/plain' if fast_reply else 'text/plain')

        # =========================
        # EXTRACT & SAVE USER NAME
        # =========================
        name_triggers = ["קוראים לי", "שמי הוא", "אני קוראים לי", "מדבר", "מדברת", "נעים מאוד אני", "זה אני"]
        for trigger in name_triggers:
            if trigger in user_text:
                try:
                    extracted_name = user_text.split(trigger)[-1].strip()
                    extracted_name = re.sub(r'^(הוא|היא|שמי|חבר|כאן)\s+', '', extracted_name)
                    extracted_name = extracted_name.replace(".", "").replace("?", "").strip()
                    if 1 <= len(extracted_name.split()) <= 3 and len(extracted_name) < 20:
                        known_name = extracted_name
                        print(f"[NAME SYSTEM] Found user name: {known_name}")
                        break
                except:
                    pass

        # =========================
        # WHAT IS MY NAME
        # =========================
        if "איך קוראים לי" in user_text or "אתה יודע מי אני" in user_text:
            if known_name:
                return Response(f"read=t-קוראים לך {known_name}={RECORD_COMMAND}", mimetype='text/plain')
            return Response(f"read=t-עדיין לא אמרת לי איך קוראים לך={RECORD_COMMAND}", mimetype='text/plain')

        # =========================
        # SYSTEM PROMPT & AI CHAT
        # =========================
        system_prompt = (
            "קוראים לך נועם. אתה עוזר קולי חכם, אנושי ומתקדם בטלפון. "
            "אתה פותחת ונבנית על ידי היוצר והמנהל שלך: שי, מומחה לניהול פרויקטים. "
            "אם המשתמש שואל 'מי פיתח אותך', 'מי יצר אותך', 'מי הבעלים שלך' או שאלות דומות, "
            "תענה בצורה מקצועית וברורה שאתה עוזר ה-AI האישי שפותח על ידי שי מניהול פרויקטים, "
            "ושתפקידך לעזור בניהול המשימות, מתן מענה וייעול התהליכים עבורו. "
            "חוק קשיח וחשוב ביותר: אם המשתמש עדיין לא הציג את עצמו (כלומר אתה לא יודע את השם שלו), "
            "במשפט הראשון שאתה עונה לו כרגע בשיחה, אתה חייב לשאול אותו בצורה נעימה וחברותית מה השם שלו לצורך השיחה! "
            "אתה יכול להרחיב בתשובות שלך, להסביר דברים לעומק ולנהל שיחה זורמת, מעניינת ומלאה – "
            "אין צורך לענות בקצר, תן תשובות מלאות ומפורטות כשצריך. "
            "אל תגיד בשום אופן שלמשתמש קוראים נועם. דבר בצורה טבעית, חברותית ומקצועית, "
            "ענה ברור ומדויק ללא סימני פיסוק כלל (כדי שההקראה הטלפונית תישמע מעולה)."
        )
        if known_name:
            system_prompt += f" השם של המשתמש שמדבר איתך כרגע הוא {known_name}."

        history.append({"role": "user", "content": user_text})

        chat = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "system", "content": system_prompt}] + history[-6:],
            temperature=0.8,
            max_tokens=300 
        )

        ai_reply = chat.choices[0].message.content.strip()
        history.append({"role": "assistant", "content": ai_reply})

        save_chat_data(caller_id, history, known_name)

        clean_reply = clean_text(ai_reply)
        print(f"AI reply: {clean_reply}")
        print(f"Response time: {time.time() - start_time:.2f} seconds")

        return Response(f"read=t-{clean_reply}={RECORD_COMMAND}", mimetype='text/plain')

    except Exception as e:
        print(f"ERROR: {e}")
        return Response(f"read=t-נועם עמוס כרגע אנא נסו שוב בעוד כמה שניות={RECORD_COMMAND}", mimetype='text/plain')

    finally:
        if tmp_filename and os.path.exists(tmp_filename):
            os.remove(tmp_filename)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)

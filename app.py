import os
import time
import tempfile
import requests
import re
import threading

from flask import Flask, request, Response
from groq import Groq

# ספריות מובנות לשליחת מייל
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

app = Flask(__name__)

# =========================
# ENV
# =========================
YEMOT_TOKEN = os.environ.get("YEMOT_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")

# הגדרות למייל (מומלץ להגדיר כמשתני סביבה)
SMTP_SERVER = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", 587))
EMAIL_ADDRESS = os.environ.get("EMAIL_ADDRESS")  # המייל שממנו נשלח
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD")  # סיסמת אפליקציה (App Password)
TARGET_EMAIL = os.environ.get("TARGET_EMAIL")  # המייל שאליו יישלח הסיכום

client = Groq(api_key=GROQ_API_KEY)

# =========================
# RECORD SETTINGS
# =========================
RECORD_COMMAND = "user_audio,no,record,,,yes,yes,no,1,20"

# =========================
# MEMORY
# =========================
conversation_memory = {}

# =========================
# CALLER NAMES
# =========================
caller_names = {}

# =========================
# CLEAN TEXT
# =========================
def clean_text(text):
    text = text.replace("**", "")
    text = text.replace("*", "")
    text = text.replace("#", "")
    text = text.replace(",", "")
    text = text.replace(".", "")
    text = text.replace("?", "")
    text = text.replace("!", "")
    text = text.replace(":", "")
    text = text.replace("-", " ")
    text = text.replace("&", " ו ")
    text = text.replace("=", " ")

    text = re.sub(r'[^\u0590-\u05FFa-zA-Z0-9\s]', '', text)
    text = " ".join(text.split())
    return text


# =========================
# QUICK ANSWERS
# =========================
def quick_answer(user_text):
    text = user_text.lower()

    if "מה השעה" in text:
        current_time = time.strftime("%H:%M")
        return f"השעה עכשיו {current_time}"

    if "מה התאריך" in text:
        current_date = time.strftime("%d/%m/%Y")
        return f"התאריך היום {current_date}"

    return None


# =========================
# SEND EMAIL FUNCTION
# =========================
def send_summary_email(caller_id, history, name):
    """פונקציה לשליחת מייל סיכום שמורצת ברקע כדי לא לתקוע את השרת"""
    if not EMAIL_ADDRESS or not EMAIL_PASSWORD or not TARGET_EMAIL:
        print("ERROR: Email configuration is missing.")
        return

    try:
        display_name = name if name else "לא ידוע"
        subject = f"סיכום שיחה מנועם עבור מספר: {caller_id} ({display_name})"
        
        # בניית גוף המייל מעוצב ב-HTML
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

        # התחברות לשרת ה-SMTP ושליחה
        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.starttls()
        server.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
        server.sendmail(EMAIL_ADDRESS, TARGET_EMAIL, msg.as_string())
        server.quit()
        print(f"Email summary sent successfully for {caller_id}")
        
    except Exception as e:
        print(f"Failed to send email: {e}")


# =========================
# MAIN ROUTE
# =========================
@app.route('/ai-chat', methods=['GET', 'POST'])
def ai_chat():
    start_time = time.time()
    caller_id = request.values.get('ApiPhone', 'unknown')

    # =========================
    # HANGUP (ניתוק השיחה)
    # =========================
    if request.values.get('hangup') == 'yes':
        # אם יש היסטוריה למספר הזה, נשלח מייל סיכום לפני שמוחקים
        if caller_id in conversation_memory and conversation_memory[caller_id]:
            history = conversation_memory[caller_id].copy()
            known_name = caller_names.get(caller_id, None)
            
            # הרצת השליחה ב-Thread נפרד כדי להחזיר תגובה מיידית לימות המשיח
            threading.Thread(
                target=send_summary_email, 
                args=(caller_id, history, known_name)
            ).start()
            
            # אופציונלי: ניקוי הזיכרון לאחר הניתוק כדי לא לצבור זבל
            del conversation_memory[caller_id]
            if caller_id in caller_names:
                del caller_names[caller_id]

        return Response("noop", mimetype='text/plain')

    # =========================
    # GET AUDIO
    # =========================
    audio_list = request.values.getlist('user_audio')
    audio_path = audio_list[-1] if audio_list else None

    # =========================
    # START CALL
    # =========================
    if not audio_path:
        return Response(
            f"read=t-שלום וברכה הגעתם לנועם במה אפשר לעזור={RECORD_COMMAND}",
            mimetype='text/plain'
        )

    print(f"Processing audio: {audio_path}")

    # =========================
    # DOWNLOAD AUDIO
    # =========================
    yemot_path = f"ivr2:{audio_path}"

    params = {
        "token": YEMOT_TOKEN,
        "path": yemot_path
    }

    tmp_filename = None

    try:
        audio_response = requests.get(
            "https://www.call2all.co.il/ym/api/DownloadFile",
            params=params,
            timeout=20
        )

        audio_response.raise_for_status()

        if len(audio_response.content) < 1000:
            return Response(
                f"read=t-לא שמעתי טוב אנא נסו שוב={RECORD_COMMAND}",
                mimetype='text/plain'
            )

        with tempfile.NamedTemporaryFile(
            suffix=".wav",
            delete=False
        ) as tmp_file:
            tmp_file.write(audio_response.content)
            tmp_filename = tmp_file.name

        # =========================
        # WHISPER
        # =========================
        with open(tmp_filename, "rb") as file:
            transcription = client.audio.transcriptions.create(
                file=file,
                model="whisper-large-v3",
                language="he"
            )

        user_text = transcription.text.strip()
        print(f"User said: {user_text}")

        if not user_text:
            return Response(
                f"read=t-לא שמעתי כלום אנא נסו שוב={RECORD_COMMAND}",
                mimetype='text/plain'
            )

        # =========================
        # QUICK ANSWERS
        # =========================
        fast_reply = quick_answer(user_text)
        if fast_reply:
            clean_reply = clean_text(fast_reply)
            return Response(
                f"read=t-{clean_reply}={RECORD_COMMAND}",
                mimetype='text/plain'
            )

        if caller_id not in conversation_memory:
            conversation_memory[caller_id] = []

        # =========================
        # SAVE NAME
        # =========================
        if "קוראים לי" in user_text:
            try:
                extracted_name = user_text.split("קוראים לי")[-1].strip()
                if len(extracted_name) < 20:
                    caller_names[caller_id] = extracted_name
            except:
                pass

        known_name = caller_names.get(caller_id)

        # =========================
        # WHAT IS MY NAME
        # =========================
        if "איך קוראים לי" in user_text:
            if known_name:
                return Response(
                    f"read=t-קוראים לך {known_name}={RECORD_COMMAND}",
                    mimetype='text/plain'
                )
            else:
                return Response(
                    f"read=t-עדיין לא אמרת לי איך קוראים לך={RECORD_COMMAND}",
                    mimetype='text/plain'
                )

        # =========================
        # SYSTEM PROMPT
        # =========================
        system_prompt = (
            "קוראים לך נועם "
            "אתה עוזר קולי חכם ואנושי בטלפון "
            "השם שלך הוא נועם "
            "אל תגיד שלמשתמש קוראים נועם "
            "דבר טבעי וחברותי "
            "ענה ברור ומדויק "
            "אל תשתמש בסימני פיסוק "
            "אל תחזור על משפטים קבועים "
            "אל תגיד אני כאן כדי לעזור "
            "אם אינך יודע תגיד שאינך יודע "
            "שמור תשובות קצרות יחסית "
        )

        if known_name:
            system_prompt += f"השם של המשתמש הוא {known_name} "

        # =========================
        # MEMORY
        # =========================
        conversation_memory[caller_id].append({
            "role": "user",
            "content": user_text
        })

        conversation_memory[caller_id] = conversation_memory[caller_id][-6:]

        # =========================
        # AI CHAT
        # =========================
        chat = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {
                    "role": "system",
                    "content": system_prompt
                }
            ] + conversation_memory[caller_id],
            temperature=0.8,
            max_tokens=120
        )

        ai_reply = chat.choices[0].message.content.strip()

        # =========================
        # SAVE AI MEMORY
        # =========================
        conversation_memory[caller_id].append({
            "role": "assistant",
            "content": ai_reply
        })

        conversation_memory[caller_id] = conversation_memory[caller_id][-6:]

        # =========================
        # CLEAN
        # =========================
        clean_reply = clean_text(ai_reply)

        print(f"AI reply: {clean_reply}")
        print(f"Response time: {time.time() - start_time:.2f} seconds")

        return Response(
            f"read=t-{clean_reply}={RECORD_COMMAND}",
            mimetype='text/plain'
        )

    except Exception as e:
        print(f"ERROR: {e}")
        return Response(
            f"read=t-נועם עמוס כרגע אנא נסו שוב בעוד כמה שניות={RECORD_COMMAND}",
            mimetype='text/plain'
        )

    finally:
        if tmp_filename and os.path.exists(tmp_filename):
            os.remove(tmp_filename)


# =========================
# RUN
# =========================
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)

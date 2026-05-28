import os
import time
import tempfile
import requests
import re

from flask import Flask, request, Response
from groq import Groq

app = Flask(__name__)

YEMOT_TOKEN = os.environ.get("YEMOT_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")

client = Groq(api_key=GROQ_API_KEY)

RECORD_COMMAND = "user_audio,no,record,,,yes,yes,no,1,60"

# import os
import time
import tempfile
import requests
import re

from flask import Flask, request, Response
from groq import Groq

app = Flask(__name__)

# =========================
# ENV
# =========================
YEMOT_TOKEN = os.environ.get("YEMOT_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")

client = Groq(api_key=GROQ_API_KEY)

RECORD_COMMAND = "user_audio,no,record,,,yes,yes,no,1,60"

# =========================
# זיכרון קצר
# =========================
conversation_memory = {}

# =========================
# שמות מתקשרים
# =========================
caller_names = {}

# =========================
# ניקוי טקסט
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
# תשובות מהירות
# חוסך טוקנים
# =========================
def quick_answer(user_text):

    text = user_text.lower()

    if "מה השעה" in text:
        current_time = time.strftime("%H:%M")
        return f"השעה עכשיו {current_time}"

    if "איזה יום היום" in text:
        current_day = time.strftime("%A")
        return f"היום {current_day}"

    if "מה התאריך" in text:
        current_date = time.strftime("%d/%m/%Y")
        return f"התאריך היום {current_date}"

    return None


@app.route('/ai-chat', methods=['GET', 'POST'])
def ai_chat():

    start_time = time.time()

    # ניתוק
    if request.values.get('hangup') == 'yes':
        return Response("noop", mimetype='text/plain')

    audio_list = request.values.getlist('user_audio')
    audio_path = audio_list[-1] if audio_list else None

    # התחלת שיחה
    if not audio_path:
        return Response(
            f"read=t-שלום וברכה הגעתם לנועם במה אפשר לעזור={RECORD_COMMAND}",
            mimetype='text/plain'
        )

    print(f"Processing audio: {audio_path}")

    yemot_path = f"ivr2:{audio_path}"

    params = {
        "token": YEMOT_TOKEN,
        "path": yemot_path
    }

    tmp_filename = None

    try:

        # =========================
        # הורדת ההקלטה
        # =========================
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

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_file:
            tmp_file.write(audio_response.content)
            tmp_filename = tmp_file.name

        # =========================
        # תמלול Whisper
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
        # תשובות מהירות
        # =========================
        fast_reply = quick_answer(user_text)

        if fast_reply:

            clean_reply = clean_text(fast_reply)

            print("Quick answer used")

            return Response(
                f"read=t-{clean_reply}={RECORD_COMMAND}",
                mimetype='text/plain'
            )

        # =========================
        # זיהוי מתקשר
        # =========================
        caller_id = request.values.get('ApiPhone', 'unknown')

        if caller_id not in conversation_memory:
            conversation_memory[caller_id] = []

        # =========================
        # שמירת שם משתמש
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
        # פרומפט חכם
        # =========================
        system_prompt = (
            "קוראים לך נועם "
            "אתה עוזר קולי חכם ואנושי בטלפון "
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
        # זיכרון שיחה
        # =========================
        conversation_memory[caller_id].append({
            "role": "user",
            "content": user_text
        })

        # רק 6 הודעות אחרונות
        conversation_memory[caller_id] = conversation_memory[caller_id][-6:]

        # =========================
        # AI
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
        # שמירת תשובת הבוט
        # =========================
        conversation_memory[caller_id].append({
            "role": "assistant",
            "content": ai_reply
        })

        conversation_memory[caller_id] = conversation_memory[caller_id][-6:]

        clean_reply = clean_text(ai_reply)

        print(f"AI reply: {clean_reply}")

        print(f"Response time: {time.time() - start_time:.2f} seconds")

        # =========================
        # תשובה לימות
        # =========================
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

        # ניקוי קובץ זמני
        if tmp_filename and os.path.exists(tmp_filename):
            os.remove(tmp_filename)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
conversation_memory = {}

# =========================
# ניקוי טקסט להקראה בימות
# =========================
def clean_text(text):

    text = text.replace("**", "").replace("*", "").replace("#", "")
    text = text.replace(",", "")
    text = text.replace(".", "")
    text = text.replace("?", "")
    text = text.replace("!", "")
    text = text.replace(":", "")
    text = text.replace("-", " ")
    text = text.replace("&", " ו ")
    text = text.replace("=", " ")

    # ניקוי תווים בעייתיים
    text = re.sub(r'[^\u0590-\u05FFa-zA-Z0-9\s]', '', text)

    # רווחים כפולים
    text = " ".join(text.split())

    return text


@app.route('/ai-chat', methods=['GET', 'POST'])
def ai_chat():

    start_time = time.time()

    # ניתוק
    if request.values.get('hangup') == 'yes':
        return Response("noop", mimetype='text/plain')

    audio_list = request.values.getlist('user_audio')
    audio_path = audio_list[-1] if audio_list else None

    # התחלת שיחה
    if not audio_path:
        return Response(
            f"read=t-שלום וברכה במה אפשר לעזור={RECORD_COMMAND}",
            mimetype='text/plain'
        )

    print(f"Processing audio: {audio_path}")

    yemot_path = f"ivr2:{audio_path}"

    params = {
        "token": YEMOT_TOKEN,
        "path": yemot_path
    }

    tmp_filename = None

    try:

        # הורדת קובץ מימות
        audio_response = requests.get(
            "https://www.call2all.co.il/ym/api/DownloadFile",
            params=params,
            timeout=20
        )

        audio_response.raise_for_status()

        # בדיקה שהקובץ תקין
        if len(audio_response.content) < 1000:
            return Response(
                f"read=t-לא שמעתי טוב אנא נסו שוב={RECORD_COMMAND}",
                mimetype='text/plain'
            )

        # שמירה זמנית
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_file:
            tmp_file.write(audio_response.content)
            tmp_filename = tmp_file.name

        # =========================
        # תמלול
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
        # זיכרון לפי מספר טלפון
        # =========================
        caller_id = request.values.get('ApiPhone', 'unknown')

        if caller_id not in conversation_memory:
            conversation_memory[caller_id] = []

        conversation_memory[caller_id].append({
            "role": "user",
            "content": user_text
        })

        # שומר רק 6 הודעות אחרונות
        conversation_memory[caller_id] = conversation_memory[caller_id][-6:]

        # =========================
        # AI
        # =========================
        chat = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "אתה עוזר קולי חכם ואנושי בטלפון "
                        "דבר טבעי וזורם "
                        "ענה ברור ומדויק "
                        "אל תשתמש בסימני פיסוק "
                        "אל תחזור על משפטים קבועים "
                        "אם אינך יודע תגיד שאינך יודע "
                        "שמור תשובות יחסית קצרות"
                    )
                }
            ] + conversation_memory[caller_id],
            temperature=0.7,
            max_tokens=150
        )

        ai_reply = chat.choices[0].message.content.strip()

        # שמירת תשובת הבוט בזיכרון
        conversation_memory[caller_id].append({
            "role": "assistant",
            "content": ai_reply
        })

        # שוב שומר רק 6 אחרונות
        conversation_memory[caller_id] = conversation_memory[caller_id][-6:]

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
            f"read=t-המערכת עמוסה כרגע אנא נסו שוב בעוד כמה שניות={RECORD_COMMAND}",
            mimetype='text/plain'
        )

    finally:

        # ניקוי קובץ זמני
        if tmp_filename and os.path.exists(tmp_filename):
            os.remove(tmp_filename)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)

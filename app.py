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

    # טיפול בניתוק
    if request.values.get('hangup') == 'yes':
        return Response("noop", mimetype='text/plain')

    audio_list = request.values.getlist('user_audio')
    audio_path = audio_list[-1] if audio_list else None

    # התחלת שיחה
    if not audio_path:
        return Response(
            f"read=t-שלום אני מאזין במה אוכל לעזור={RECORD_COMMAND}",
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
        # הורדת ההקלטה מימות
        audio_response = requests.get(
            "https://www.call2all.co.il/ym/api/DownloadFile",
            params=params,
            timeout=20
        )

        audio_response.raise_for_status()

        # בדיקת קובץ תקין
        if len(audio_response.content) < 1000:
            return Response(
                f"read=t-ההקלטה לא התקבלה טוב אנא נסו שוב={RECORD_COMMAND}",
                mimetype='text/plain'
            )

        # שמירה זמנית
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
        # AI RESPONSE
        # =========================
        chat = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "אתה עוזר קולי חכם בטלפון "
                        "ענה בקצרה מאוד "
                        "בלי סימני פיסוק "
                        "בלי אימוגים "
                        "עד שני משפטים קצרים"
                    )
                },
                {
                    "role": "user",
                    "content": user_text
                }
            ],
            temperature=0.5,
            max_tokens=120
        )

        ai_reply = chat.choices[0].message.content.strip()

        clean_reply = clean_text(ai_reply)

        print(f"AI reply: {clean_reply}")

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

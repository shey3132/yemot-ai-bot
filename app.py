import os
import time
import tempfile
import requests
import re

from flask import Flask, request, Response
from groq import Groq
from gtts import gTTS

app = Flask(__name__)

# =========================
# ENV
# =========================
YEMOT_TOKEN = os.environ.get("YEMOT_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")

client = Groq(api_key=GROQ_API_KEY)

# =========================
# RECORD SETTINGS
# =========================
RECORD_COMMAND = "user_audio,no,record,,,yes,yes,no,1,20"

# =========================
# MEMORY
# =========================
conversation_memory = {}
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
        return f"השעה עכשיו {time.strftime('%H:%M')}"

    if "מה התאריך" in text:
        return f"התאריך היום {time.strftime('%d/%m/%Y')}"

    return None


# =========================
# TTS (gTTS - חינמי ויציב)
# =========================
def generate_tts(text):

    output_file = tempfile.NamedTemporaryFile(
        suffix=".mp3",
        delete=False
    )

    tts = gTTS(text=text, lang="he")
    tts.save(output_file.name)

    return output_file.name


# =========================
# UPLOAD TO YEMOT
# =========================
def upload_to_yemot(file_path, filename):

    url = "https://www.call2all.co.il/ym/api/UploadFile"

    with open(file_path, "rb") as f:

        files = {"file": f}

        data = {
            "token": YEMOT_TOKEN,
            "path": f"ivr2:/tts/{filename}"
        }

        response = requests.post(url, files=files, data=data)

    print("UPLOAD:", response.text)

    return f"f-/ivr2/tts/{filename.replace('.mp3','')}"


# =========================
# MAIN ROUTE
# =========================
@app.route('/ai-chat', methods=['GET', 'POST'])
def ai_chat():

    start_time = time.time()

    if request.values.get('hangup') == 'yes':
        return Response("noop", mimetype='text/plain')

    audio_list = request.values.getlist('user_audio')
    audio_path = audio_list[-1] if audio_list else None

    if not audio_path:

        return Response(
            f"read=t-שלום וברכה הגעתם לנועם במה אפשר לעזור={RECORD_COMMAND}",
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

        if len(audio_response.content) < 1000:
            return Response(
                f"read=t-לא שמעתי טוב אנא נסו שוב={RECORD_COMMAND}",
                mimetype='text/plain'
            )

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp.write(audio_response.content)
            tmp_filename = tmp.name

        with open(tmp_filename, "rb") as file:
            transcription = client.audio.transcriptions.create(
                file=file,
                model="whisper-large-v3",
                language="he"
            )

        user_text = transcription.text.strip()

        if not user_text:
            return Response(
                f"read=t-לא שמעתי כלום={RECORD_COMMAND}",
                mimetype='text/plain'
            )

        fast_reply = quick_answer(user_text)

        if fast_reply:

            clean_reply = clean_text(fast_reply)

            tts_file = generate_tts(clean_reply)
            filename = f"quick_{int(time.time())}.mp3"

            uploaded = upload_to_yemot(tts_file, filename)

            os.remove(tts_file)

            return Response(
                f"id_list_message={uploaded}={RECORD_COMMAND}",
                mimetype='text/plain'
            )

        caller_id = request.values.get('ApiPhone', 'unknown')

        if caller_id not in conversation_memory:
            conversation_memory[caller_id] = []

        if "קוראים לי" in user_text:
            try:
                name = user_text.split("קוראים לי")[-1].strip()
                if len(name) < 20:
                    caller_names[caller_id] = name
            except:
                pass

        known_name = caller_names.get(caller_id)

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

        system_prompt = "אתה עוזר קולי בשם נועם ענה בעברית קצרה וברורה"

        if known_name:
            system_prompt += f"השם של המשתמש הוא {known_name} "

        conversation_memory[caller_id].append({
            "role": "user",
            "content": user_text
        })

        conversation_memory[caller_id] = conversation_memory[caller_id][-6:]

        chat = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "system", "content": system_prompt}]
            + conversation_memory[caller_id],
            temperature=0.8,
            max_tokens=120
        )

        ai_reply = chat.choices[0].message.content.strip()

        conversation_memory[caller_id].append({
            "role": "assistant",
            "content": ai_reply
        })

        conversation_memory[caller_id] = conversation_memory[caller_id][-6:]

        clean_reply = clean_text(ai_reply)

        tts_file = generate_tts(clean_reply)

        filename = f"{caller_id}_{int(time.time())}.mp3"
        uploaded = upload_to_yemot(tts_file, filename)

        os.remove(tts_file)

        print(f"Response time: {time.time() - start_time:.2f}s")

        return Response(
            f"id_list_message={uploaded}={RECORD_COMMAND}",
            mimetype='text/plain'
        )

    except Exception as e:

        print("ERROR:", e)

        return Response(
            f"read=t-בעיה במערכת נסו שוב={RECORD_COMMAND}",
            mimetype='text/plain'
        )

    finally:

        if tmp_filename and os.path.exists(tmp_filename):
            os.remove(tmp_filename)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)

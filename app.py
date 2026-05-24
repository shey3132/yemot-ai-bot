import os
import tempfile
import asyncio
import edge_tts
import requests
from flask import Flask, request
import google.generativeai as genai

app = Flask(__name__)

YEMOT_TOKEN = os.environ.get("YEMOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-2.5-flash')

sessions = {}

# פונקציה להמרת טקסט לאודיו בעזרת המנוע של Microsoft Edge
async def text_to_speech(text, output_file):
    # 'he-IL-AvriNeural' הוא קול גבר, 'he-IL-HilaNeural' הוא קול אישה
    communicate = edge_tts.Communicate(text, "he-IL-AvriNeural")
    await communicate.save(output_file)

@app.route('/ai-chat', methods=['GET', 'POST'])
def ai_chat():
    data = request.form if request.method == 'POST' else request.args
    user_id = data.get('ApiPhone') or data.get('ApiCallId', 'unknown')
    audio_path = data.get('user_audio')

    if not audio_path:
        if user_id not in sessions:
            sessions[user_id] = [{"role": "user", "parts": ["אתה עוזר קולי."]}, {"role": "model", "parts": ["שלום."]}]
        return "id_list_message=t-שלום, במה אוכל לעזור?&read=t-אנא דבר=user_audio,no,record,,,,,15,,"

    # 1. קבלת אודיו מהמשתמש
    download_url = "https://www.call2all.co.il/ym/api/DownloadFile"
    params = {"token": YEMOT_TOKEN, "path": f"ivr2:{audio_path}"}
    audio_response = requests.get(download_url, params=params)
    
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_file:
        tmp_file.write(audio_response.content)
        tmp_filename = tmp_file.name

    try:
        audio_file = genai.upload_file(path=tmp_filename)
        if user_id not in sessions: sessions[user_id] = []
        sessions[user_id].append({"role": "user", "parts": [audio_file]})
        response = model.generate_content(sessions[user_id])
        ai_reply = response.text
        sessions[user_id].append({"role": "model", "parts": [ai_reply]})

        # 2. הפיכת הטקסט לקול טבעי
        speech_file = tmp_filename + ".mp3"
        asyncio.run(text_to_speech(ai_reply, speech_file))

        # 3. העלאת הקובץ לימות המשיח
        with open(speech_file, 'rb') as f:
            upload_url = "https://www.call2all.co.il/ym/api/UploadFile"
            requests.post(upload_url, params={"token": YEMOT_TOKEN, "path": "/ApiRecord/reply.mp3"}, files={'file': f})

        if os.path.exists(speech_file): os.remove(speech_file)
        
        return "id_list_message=t-/ApiRecord/reply.mp3&read=t-המשך=user_audio,no,record,,,,,15,,"

    except Exception as e:
        return "id_list_message=t-שגיאה בעיבוד&read=t-המשך=user_audio,no,record,,,,,15,,"
    finally:
        if os.path.exists(tmp_filename): os.remove(tmp_filename)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)

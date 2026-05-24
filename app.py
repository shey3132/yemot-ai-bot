import os
import tempfile
import requests
from flask import Flask, request
import google.generativeai as genai

app = Flask(__name__)

YEMOT_TOKEN = os.environ.get("YEMOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-2.5-flash')

sessions = {}

@app.route('/ai-chat', methods=['GET', 'POST'])
def ai_chat():
    data = request.form if request.method == 'POST' else request.args
    user_phone = data.get('ApiPhone')
    user_id = user_phone if user_phone else data.get('ApiCallId', 'unknown')
    audio_path = data.get('user_audio')

    if not audio_path:
        if user_id not in sessions:
            sessions[user_id] = [{"role": "user", "parts": ["אתה עוזר קולי. ענה קצר."]}, {"role": "model", "parts": ["שלום."]}]
        return "id_list_message=t-שלום, במה אוכל לעזור?&read=t-אנא דבר=user_audio,no,record,,,,,15,,"

    params = {"token": YEMOT_TOKEN, "path": f"ivr2:{audio_path}"}
    audio_response = requests.get("https://www.call2all.co.il/ym/api/DownloadFile", params=params)
    
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
        
        clean_reply = ai_reply.replace("&", " ו").replace("=", " שווה ").replace("*", "").replace("#", "")
        return f"id_list_message=t-{clean_reply}&read=t-המשך=user_audio,no,record,,,,,15,,"
    except Exception as e:
        return "id_list_message=t-קרתה תקלה. נסה שוב.&read=t-אנא דבר=user_audio,no,record,,,,,15,,"
    finally:
        if os.path.exists(tmp_filename): os.remove(tmp_filename)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)

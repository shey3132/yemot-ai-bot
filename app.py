import os
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
    user_id = data.get('ApiPhone') or data.get('ApiCallId', 'unknown')
    audio_path = data.get('user_audio')

    if not audio_path:
        return "id_list_message=t-שלום, אני מוכן. אנא דבר.&read=t-הקלטה=user_audio,no,record,,,,,15,,"

    # הורדת האודיו
    params = {"token": YEMOT_TOKEN, "path": f"ivr2:{audio_path}"}
    audio_response = requests.get("https://www.call2all.co.il/ym/api/DownloadFile", params=params)
    
    # שליחה לג'מיני (בלי לשמור קבצים מיותרים ב-Render)
    audio_file = genai.upload_file(content=audio_response.content, mime_type="audio/wav")
    
    if user_id not in sessions: sessions[user_id] = []
    sessions[user_id].append({"role": "user", "parts": [audio_file]})
    
    response = model.generate_content(sessions[user_id])
    ai_reply = response.text
    sessions[user_id].append({"role": "model", "parts": [ai_reply]})
    
    # פיסוק לשיפור הקול
    clean_reply = ai_reply.replace("&", " ו").replace("=", " שווה ").replace(".", ". ,").replace("!", "! ,")
    
    # החזרת תשובה בלבד - ללא read שקוטע את ההקראה
    return f"id_list_message=t-{clean_reply}"

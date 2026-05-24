import os
import tempfile
import requests
from flask import Flask, request
import google.generativeai as genai

app = Flask(__name__)

# משיכת מפתחות ממשתני הסביבה ב-Render
YEMOT_TOKEN = os.environ.get("YEMOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-1.5-flash')

SYSTEM_PROMPT = """You are an intelligent voice agent managing interactive phone conversations. Your goal is to provide accurate, concise, and direct responses to maintain a fast, natural, and fluid real-time communication pace.

Operational guidelines:
1. Answer directly with brevity and precision. Avoid filler and unnecessary introductions.
2. Optimize for audio. Use clear conversational language and avoid symbols or formatting that do not work well in text to speech.
3. If a response gets long, summarize the core points and keep the pace efficient.
4. Keep a professional, helpful, and courteous tone.
5. If information is unavailable, state that briefly and neutrally.

Response rules:
- Reply in plain text only.
- Avoid special characters or formatting that does not translate well to speech.
- For complex topics, use short and simple sentences.
- Act only on professional information provided in the conversation context."""

# ניהול היסטוריית שיחות
sessions = {}

@app.route('/ai-chat', methods=['GET', 'POST'])
def ai_chat():
    data = request.form if request.method == 'POST' else request.args
    call_id = data.get('ApiCallId')
    audio_path = data.get('user_audio')

    if not audio_path:
        sessions[call_id] = [
            {"role": "user", "parts": [SYSTEM_PROMPT]},
            {"role": "model", "parts": ["Hello, I am ready to help."]}
        ]
        return "id_list_message=t-שלום, אני מודל ג'מיני, במה אוכל לעזור?&read=t-אנא דבר אחרי הצפצוף=user_audio,no,record,,,,,15,,"

    # הורדת הקובץ עם Headers לזיהוי תקין
    download_url = f"https://www.call2all.co.il/ym/api/DownloadFile?token={YEMOT_TOKEN}&path={audio_path}"
    headers = {'User-Agent': 'Mozilla/5.0'}
    audio_response = requests.get(download_url, headers=headers)
    
    if audio_response.status_code != 200:
        return "id_list_message=t-שגיאה בקבלת הקובץ&read=t-אנא נסה שוב=user_audio,no,record,,,,,15,,"

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_file:
        tmp_file.write(audio_response.content)
        tmp_filename = tmp_file.name

    try:
        audio_file = genai.upload_file(path=tmp_filename)
        if call_id not in sessions:
            sessions[call_id] = [{"role": "user", "parts": [SYSTEM_PROMPT]}, {"role": "model", "parts": ["Understood."]}]
        
        sessions[call_id].append({"role": "user", "parts": [audio_file]})
        response = model.generate_content(sessions[call_id])
        ai_reply = response.text
        sessions[call_id].append({"role": "model", "parts": [ai_reply]})
        audio_file.delete()
    except Exception as e:
        ai_reply = "קרתה תקלה בעיבוד."
    finally:
        if os.path.exists(tmp_filename):
            os.remove(tmp_filename)

    clean_reply = ai_reply.replace("&", " ו").replace("=", " שווה ").replace("*", "").replace("#", "")
    return f"id_list_message=t-{clean_reply}&read=t-המשך לדבר=user_audio,no,record,,,,,15,,"

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)

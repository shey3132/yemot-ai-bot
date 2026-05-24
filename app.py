import os
import tempfile
import requests
from flask import Flask, request, Response
import google.generativeai as genai

app = Flask(__name__)

# משיכת מפתחות
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
            sessions[user_id] = [{"role": "user", "parts": ["אתה עוזר קולי. ענה קצר ותמציתי."]}, {"role": "model", "parts": ["שלום."]}]
        
        # שימוש ב-Response כדי להבטיח Plain Text לימות המשיח
        return Response("read=t-שלום אני מוכן אנא דבר=user_audio,no,record,,,,,15,,", mimetype='text/plain')

    # הורדת הקובץ מימות המשיח
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
        
        # 1. ניקוי תווים שבורים
        clean_reply = ai_reply.replace("&", " ו").replace("=", " שווה ").replace("*", "").replace("#", "")
        clean_reply = clean_reply.replace(",", " ").replace("-", " ")
        
        # 2. התיקון הקריטי: מחיקת כל ירידות השורה (Enters) כדי שזה יהיה שורה אחת בלבד!
        clean_reply = clean_reply.replace("\n", " ").replace("\r", " ")
        
        # 3. לפי המדריך של ימות המשיח: אנחנו משתמשים בפקודת read שגם משמיעה וגם מקליטה מיד
        api_response = f"read=t-{clean_reply}=user_audio,no,record,,,,,15,,"
        
        # 4. החזרת טקסט נקי בלבד (Plain Text)
        return Response(api_response, mimetype='text/plain')

    except Exception as e:
        print(f"DEBUG: Error: {e}")
        return Response("read=t-קרתה תקלה נסה שוב=user_audio,no,record,,,,,15,,", mimetype='text/plain')
    finally:
        if os.path.exists(tmp_filename): os.remove(tmp_filename)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)

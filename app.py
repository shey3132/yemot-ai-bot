import os
import tempfile
import requests
from flask import Flask, request, Response
import google.generativeai as genai

app = Flask(__name__)

# הגדרות סביבה
YEMOT_TOKEN = os.environ.get("YEMOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

genai.configure(api_key=GEMINI_API_KEY)

# הגדרת המודל - בצורה שתואמת את ה-SDK החדש
model = genai.GenerativeModel(
    model_name='gemini-2.5-flash',
    tools=["google_search"]
)

sessions = {}

@app.route('/ai-chat', methods=['GET', 'POST'])
def ai_chat():
    data = request.form if request.method == 'POST' else request.args
    user_phone = data.get('ApiPhone', 'unknown')
    audio_path = data.get('user_audio')

    # התחלת שיחה
    if not audio_path:
        return Response("read=t-שלום, אני מאזין. במה אוכל לעזור?=user_audio,no,record,,,,,15,,", mimetype='text/plain')

    # הורדת הקובץ מימות המשיח
    params = {"token": YEMOT_TOKEN, "path": f"ivr2:{audio_path}"}
    try:
        audio_response = requests.get("https://www.call2all.co.il/ym/api/DownloadFile", params=params)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_file:
            tmp_file.write(audio_response.content)
            tmp_filename = tmp_file.name
    except Exception:
        return Response("read=t-שגיאה בהורדת קובץ=user_audio,no,record,,,,,15,,", mimetype='text/plain')

    try:
        audio_file = genai.upload_file(path=tmp_filename)
        
        # יצירת תשובה מה-AI
        response = model.generate_content(["אתה עוזר קולי חכם, ענה בקיצור ובבירור לעניין.", audio_file])
        ai_reply = response.text
        
        # ניקוי הטקסט לימות המשיח
        clean_reply = ai_reply.replace("&", " ").replace("=", " ").replace("*", "").replace("#", "")
        clean_reply = " ".join(clean_reply.split())
        
        return Response(f"read=t-{clean_reply}=user_audio,no,record,,,,,15,,", mimetype='text/plain')

    except Exception as e:
        return Response("read=t-קרתה תקלה, נסה שוב=user_audio,no,record,,,,,15,,", mimetype='text/plain')
    
    finally:
        if 'tmp_filename' in locals() and os.path.exists(tmp_filename): os.remove(tmp_filename)
        if 'audio_file' in locals():
            try: genai.delete_file(audio_file.name)
            except: pass

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)

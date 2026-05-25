import os
import tempfile
import requests
from flask import Flask, request, Response
import google.generativeai as genai

app = Flask(__name__)

# פונקציה שתטען את המודל רק כשצריך (מונע קריסה בהפעלה)
def get_model():
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return None
    genai.configure(api_key=key)
    # שימוש ברשימה פשוטה כדי למנוע שגיאות
    return genai.GenerativeModel(model_name='gemini-1.5-flash', tools=["google_search"])

@app.route('/ai-chat', methods=['GET', 'POST'])
def ai_chat():
    data = request.form if request.method == 'POST' else request.args
    audio_path = data.get('user_audio')

    if not audio_path:
        return Response("read=t-שלום, אני מאזין.=user_audio,no,record,,,,,15,,", mimetype='text/plain')

    model = get_model()
    if not model:
        return Response("read=t-חסרה הגדרת מפתח API=user_audio,no,record,,,,,15,,", mimetype='text/plain')

    # הורדת הקובץ
    params = {"token": os.environ.get("YEMOT_TOKEN"), "path": f"ivr2:{audio_path}"}
    try:
        audio_response = requests.get("https://www.call2all.co.il/ym/api/DownloadFile", params=params)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_file:
            tmp_file.write(audio_response.content)
            tmp_filename = tmp_file.name
    except Exception:
        return Response("read=t-שגיאת רשת=user_audio,no,record,,,,,15,,", mimetype='text/plain')

    try:
        audio_file = genai.upload_file(path=tmp_filename)
        response = model.generate_content(["ענה בקצרה ובבירור.", audio_file])
        
        clean_reply = response.text.replace("&", " ").replace("=", " ").replace("*", "").replace("#", "")
        clean_reply = " ".join(clean_reply.split())
        
        return Response(f"read=t-{clean_reply}=user_audio,no,record,,,,,15,,", mimetype='text/plain')

    except Exception as e:
        return Response("read=t-קרתה תקלה=user_audio,no,record,,,,,15,,", mimetype='text/plain')
    
    finally:
        if 'tmp_filename' in locals() and os.path.exists(tmp_filename): os.remove(tmp_filename)
        if 'audio_file' in locals():
            try: genai.delete_file(audio_file.name)
            except: pass

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)

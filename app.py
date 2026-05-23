import os
import tempfile
import requests
from flask import Flask, request
import google.generativeai as genai

app = Flask(__name__)

# משיכת מפתחות ממשתני הסביבה
YEMOT_TOKEN = os.environ.get("YEMOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-1.5-flash')

# ניהול היסטוריית שיחות
sessions = {}

@app.route('/ai-chat', methods=['GET', 'POST'])
def ai_chat():
    data = request.form if request.method == 'POST' else request.args
    call_id = data.get('ApiCallId')
    audio_path = data.get('user_audio')

    if not audio_path:
        sessions[call_id] = [
            {"role": "user", "parts": ["אתה עוזר קולי בעברית. ענה קצר, ברור וללא סימנים מיוחדים."]},
            {"role": "model", "parts": ["שלום, אני מוכן לעזור."]}
        ]
        return "id_list_message=t-שלום, אני מודל ג'מיני, במה אוכל לעזור?&read=t-אנא דבר אחרי הצפצוף=user_audio,no,record,,,,,15,,"

    download_url = f"https://www.call2all.co.il/ym/api/DownloadFile?token={YEMOT_TOKEN}&path={audio_path}"
    audio_response = requests.get(download_url)
    
    if audio_response.status_code != 200:
        return "id_list_message=t-שגיאה בקבלת הקובץ&read=t-אנא נסה שוב=user_audio,no,record,,,,,15,,"

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_file:
        tmp_file.write(audio_response.content)
        tmp_filename = tmp_file.name

    try:
        audio_file = genai.upload_file(path=tmp_filename)
        sessions[call_id].append({"role": "user", "parts": [audio_file]})
        response = model.generate_content(sessions[call_id])
        ai_reply = response.text
        sessions[call_id].append({"role": "model", "parts": [ai_reply]})
        audio_file.delete()
    except Exception as e:
        ai_reply = "קרתה תקלה בעיבוד."

    if os.path.exists(tmp_filename):
        os.remove(tmp_filename)

    clean_reply = ai_reply.replace("&", " ו").replace("=", " שווה ").replace("*", "").replace("#", "")
    return f"id_list_message=t-{clean_reply}&read=t-המשך לדבר=user_audio,no,record,,,,,15,,"

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)

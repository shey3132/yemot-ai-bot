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
model = genai.GenerativeModel('gemini-2.5-flash')

# ניהול היסטוריית שיחות לפי מספר טלפון
sessions = {}

@app.route('/ai-chat', methods=['GET', 'POST'])
def ai_chat():
    data = request.form if request.method == 'POST' else request.args
    
    # שינוי קריטי: מזהה המשתמש יהיה מספר הטלפון שלו במקום מזהה השיחה הזמני
    user_phone = data.get('ApiPhone')
    # במקרה שאין מספר טלפון (חסוי), נשתמש במזהה השיחה כגיבוי
    user_id = user_phone if user_phone else data.get('ApiCallId', 'unknown')
    
    audio_path = data.get('user_audio')

    if not audio_path:
        # אם זו פעם ראשונה שהמספר הזה מתקשר, נפתח לו היסטוריה חדשה
        if user_id not in sessions:
            sessions[user_id] = [
                {"role": "user", "parts": ["אתה עוזר קולי. ענה קצר, ברור וללא סימנים מיוחדים."]},
                {"role": "model", "parts": ["שלום, אני מוכן לעזור."]}
            ]
        return "id_list_message=t-שלום, אני מודל ג'מיני, במה אוכל לעזור?&read=t-אנא דבר אחרי הצפצוף=user_audio,no,record,,,,,15,,"

    # הוספת הקידומת הנדרשת לימות המשיח
    full_audio_path = f"ivr2:{audio_path}"
    
    download_url = "https://www.call2all.co.il/ym/api/DownloadFile"
    
    params = {
        "token": YEMOT_TOKEN,
        "path": full_audio_path
    }
    headers = {'User-Agent': 'Mozilla/5.0'}
    
    audio_response = requests.get(download_url, params=params, headers=headers)
    
    if audio_response.status_code != 200 or "Requested file does not exist" in audio_response.text:
        print(f"DEBUG: Download Error. Status: {audio_response.status_code}, Response: {audio_response.text}")
        return "id_list_message=t-שגיאה בקבלת הקובץ&read=t-אנא נסה שוב=user_audio,no,record,,,,,15,,"

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_file:
        tmp_file.write(audio_response.content)
        tmp_filename = tmp_file.name

    try:
        audio_file = genai.upload_file(path=tmp_filename)
        
        # וידוא שיש למשתמש הזה היסטוריה
        if user_id not in sessions:
            sessions[user_id] = [{"role": "user", "parts": ["אתה עוזר קולי. ענה קצר וללא סימנים."]}, {"role": "model", "parts": ["כן."]}]
        
        # הוספת קובץ האודיו להיסטוריה האישית של המספר הזה ושליחה לגוגל
        sessions[user_id].append({"role": "user", "parts": [audio_file]})
        response = model.generate_content(sessions[user_id])
        ai_reply = response.text
        
        # שמירת תשובת המודל להיסטוריה האישית
        sessions[user_id].append({"role": "model", "parts": [ai_reply]})
        
    except Exception as e:
        print(f"DEBUG: Gemini API Error: {e}")
        ai_reply = "קרתה תקלה בעיבוד מול גוגל."
    finally:
        # מחיקת הקובץ הזמני מהשרת (Render)
        if os.path.exists(tmp_filename):
            os.remove(tmp_filename)

    clean_reply = ai_reply.replace("&", " ו").replace("=", " שווה ").replace("*", "").replace("#", "")
    
    return f"id_list_message=t-{clean_reply}&read=t-המשך לדבר=user_audio,no,record,,,,,15,,"

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)

import os
import time
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

# ניהול היסטוריית שיחות
sessions = {}

@app.route('/ai-chat', methods=['GET', 'POST'])
def ai_chat():
    data = request.form if request.method == 'POST' else request.args
    call_id = data.get('ApiCallId')
    audio_path = data.get('user_audio')

    if not audio_path:
        sessions[call_id] = [
            {"role": "user", "parts": ["אתה עוזר קולי. ענה קצר, ברור וללא סימנים מיוחדים."]},
            {"role": "model", "parts": ["שלום, אני מוכן לעזור."]}
        ]
        return "id_list_message=t-שלום, אני מודל ג'מיני, במה אוכל לעזור?&read=t-אנא דבר אחרי הצפצוף=user_audio,no,record,,,,,15,,"

    # בסיס הכתובת להורדה בימות המשיח
    download_url = "https://www.call2all.co.il/ym/api/DownloadFile"
    
    # שליחת הפרמטרים בצורת דיקשנרי שמבצעת קידוד (URL Encoding) אוטומטי ובטוח
    params = {
        "token": YEMOT_TOKEN,
        "path": audio_path
    }
    headers = {'User-Agent': 'Mozilla/5.0'}
    
    audio_response = None
    
    # מנגנון שריון: מנסה להוריד את הקובץ עד 3 פעמים עם השהייה קלה בין ניסיון לניסיון
    for attempt in range(3):
        try:
            audio_response = requests.get(download_url, params=params, headers=headers)
            # בדיקה שהסטטוס הוא 200 ושהתשובה לא מכילה הודעת שגיאה טקסטואלית של ימות המשיח
            if audio_response.status_code == 200 and "Requested file does not exist" not in audio_response.text:
                print(f"DEBUG: File downloaded successfully on attempt {attempt + 1}")
                break
        except Exception as e:
            print(f"DEBUG: Attempt {attempt + 1} failed with exception: {e}")
            
        print(f"DEBUG: File not ready on attempt {attempt + 1}. Waiting...")
        time.sleep(1.0)  # ממתין שנייה אחת מלאה לפני הניסיון הבא כדי לתת לשרת של ימות המשיח זמן להתאושש

    # אם אחרי 3 ניסיונות עדיין יש שגיאה
    if not audio_response or audio_response.status_code != 200 or "Requested file does not exist" in audio_response.text:
        print(f"DEBUG: Final failure. Status: {audio_response.status_code if audio_response else 'None'}, Response: {audio_response.text if audio_response else 'None'}")
        return "id_list_message=t-שגיאה בקבלת הקובץ&read=t-אנא נסה שוב=user_audio,no,record,,,,,15,,"

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_file:
        tmp_file.write(audio_response.content)
        tmp_filename = tmp_file.name

    try:
        audio_file = genai.upload_file(path=tmp_filename)
        if call_id not in sessions:
            sessions[call_id] = [{"role": "user", "parts": ["אתה עוזר קולי. ענה קצר וללא סימנים."]}, {"role": "model", "parts": ["כן."]}]
        
        sessions[call_id].append({"role": "user", "parts": [audio_file]})
        response = model.generate_content(sessions[call_id])
        ai_reply = response.text
        sessions[call_id].append({"role": "model", "parts": [ai_reply]})
        audio_file.delete()
    except Exception as e:
        print(f"DEBUG: Gemini API Error: {e}")
        ai_reply = "קרתה תקלה בעיבוד."
    finally:
        if os.path.exists(tmp_filename):
            os.remove(tmp_filename)

    clean_reply = ai_reply.replace("&", " ו").replace("=", " שווה ").replace("*", "").replace("#", "")
    return f"id_list_message=t-{clean_reply}&read=t-המשך לדבר=user_audio,no,record,,,,,15,,"

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)

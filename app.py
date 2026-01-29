from flask import Flask, request, make_response
import os
import requests
import google.generativeai as genai
import tempfile

app = Flask(__name__)

# הגדרת מפתחות
GOOGLE_API_KEY = os.environ.get("GEMINI_API_KEY")
genai.configure(api_key=GOOGLE_API_KEY)
YEMOT_TOKEN = os.environ.get("YEMOT_TOKEN")

@app.route('/ask_ai', methods=['GET', 'POST'])
def ask_ai():
    # קבלת נתיב ההקלטה מימות המשיח
    audio_path = request.values.get('path')

    # שלב א: אם אין הקלטה - מבקשים להקליט
    if not audio_path:
        # טקסט נקי לחלוטין ללא פסיקים או תווים מיוחדים
        response_text = "read=t-נא להגיד את השאלה לאחר הצפצוף ובסיום להקיש סולמית&target=path&max=30&beep=yes"
        return response_text

    # שלב ב: יש הקלטה - מעבדים אותה
    try:
        # 1. הורדת הקובץ מימות המשיח
        download_url = f"https://www.call2all.co.il/ym/api/DownloadFile?token={YEMOT_TOKEN}&path={audio_path}"
        response = requests.get(download_url)
        
        if response.status_code != 200:
            return "id_list_message=t-חלה שגיאה בהורדת הקובץ"

        # 2. שמירה זמנית ושליחה ל-Gemini
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp_audio:
            temp_audio.write(response.content)
            temp_audio_path = temp_audio.name

        # העלאה ל-AI
        model = genai.GenerativeModel("gemini-1.5-flash")
        sample_file = genai.upload_file(path=temp_audio_path, mime_type="audio/wav")
        
        # בקשת תשובה
        answer = model.generate_content([
            "הקשב להקלטה וענה בעברית בצורה תמציתית וברורה", 
            sample_file
        ])
        
        # ניקוי התשובה מסימנים שמשבשים את הקריין
        clean_answer = answer.text.replace("*", "").replace(">", "").replace("\n", " ")
        
        return f"id_list_message=t-{clean_answer}"

    except Exception as e:
        print(f"Error: {e}")
        return "id_list_message=t-חלה שגיאה בעיבוד הנתונים"

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)

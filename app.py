from flask import Flask, request
import os
import requests
import google.generativeai as genai
import tempfile

app = Flask(__name__)

# הגדרת מפתחות
genai.configure(api_key=os.environ.get("GEMINI_API_KEY"))
YEMOT_TOKEN = os.environ.get("YEMOT_TOKEN")

@app.route('/ask_ai', methods=['GET', 'POST'])
def ask_ai():
    # בדיקה האם ימות המשיח שלחו נתיב של קובץ שמע
    audio_path = request.values.get('path')

    # אם אין נתיב (כלומר המשתמש הרגע נכנס לשלוחה) - אנחנו מבקשים ממנו להקליט
    if not audio_path:
        # זו השורה ששאלת עליה - היא חייבת להיות כאן בתוך ה-if
        return "read=t-נא לדבר אחרי הצפצוף ובסיום סולמית&target=path&max=20&beep=yes"

    # אם יש נתיב - ממשיכים לעיבוד עם ה-AI
    try:
        download_url = f"https://www.call2all.co.il/ym/api/DownloadFile?token={YEMOT_TOKEN}&path={audio_path}"
        response = requests.get(download_url)
        
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp_audio:
            temp_audio.write(response.content)
            temp_audio_path = temp_audio.name

        model = genai.GenerativeModel("gemini-1.5-flash")
        sample_file = genai.upload_file(path=temp_audio_path, mime_type="audio/wav")
        answer = model.generate_content(["תענה בקצרה בעברית", sample_file])
        
        # ניקוי התשובה מסימנים שמשבשים את המערכת
        clean_answer = "".join(c for c in answer.text if c.isalnum() or c in " .")
        
        return f"id_list_message=t-{clean_answer}"

    except Exception as e:
        return "id_list_message=t-חלה שגיאה בעיבוד השאלה"

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)

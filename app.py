from flask import Flask, request
import os
import requests
import google.generativeai as genai
import tempfile

app = Flask(__name__)

# הגדרת מפתחות מתוך משתני הסביבה של Render
GOOGLE_API_KEY = os.environ.get("GEMINI_API_KEY")
genai.configure(api_key=GOOGLE_API_KEY)
YEMOT_TOKEN = os.environ.get("YEMOT_TOKEN")

@app.route('/ask_ai', methods=['GET', 'POST'])
def ask_ai():
    # קבלת נתיב הקובץ שהוקלט (אם קיים)
    audio_path = request.values.get('path')

    # שלב א: אם המערכת רק נכנסה ואין עדיין הקלטה
    if not audio_path:
        # פקודת הקלטה "סטרילית" - בלי פסיקים, בלי גרשיים מיותרים ובלי סימני < >
        # הטקסט שיושמע: "נא להגיד את השאלה ולאחר מכן להקיש סולמית"
        return "read=t-נא להגיד את השאלה ולאחר מכן להקיש סולמית&target=path&max=20&beep=yes"

    # שלב ב: יש הקלטה, שולחים אותה לבינה המלאכותית
    try:
        # הורדת הקובץ מימות המשיח
        download_url = f"https://www.call2all.co.il/ym/api/DownloadFile?token={YEMOT_TOKEN}&path={audio_path}"
        response = requests.get(download_url)
        
        if response.status_code != 200:
            return "id_list_message=t-תקלה בהורדת ההקלטה"

        # שמירת הקובץ זמנית לצורך העלאה לגוגל
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp_audio:
            temp_audio.write(response.content)
            temp_audio_path = temp_audio.name

        # הגדרת המודל והעלאת הקובץ
        model = genai.GenerativeModel("gemini-1.5-flash")
        sample_file = genai.upload_file(path=temp_audio_path, mime_type="audio/wav")
        
        # קבלת תשובה מה-AI
        answer = model.generate_content([
            "ענה על השאלה שבהקלטה בעברית קצרה ופשוטה. אל תשתמש בסימנים מיוחדים כמו כוכביות או סולמיות.", 
            sample_file
        ])
        
        # ניקוי התשובה מסימנים שעלולים לשבש את הקריין של ימות המשיח
        clean_answer = answer.text.replace("*", "").replace(">", "").replace("\n", " ").replace("\"", "")
        
        # השמעת התשובה וניתוק/סיום
        return f"id_list_message=t-{clean_answer}"

    except Exception as e:
        print(f"Error: {e}")
        return "id_list_message=t-חלה שגיאה בעיבוד השאלה"

if __name__ == '__main__':
    # הרצה על הפורט ש-Render דורש
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)

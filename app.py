from flask import Flask, request
import os, requests, tempfile
import google.generativeai as genai

app = Flask(__name__)

# הגדרת מפתח Gemini (מומלץ להגדיר ב-Render כפי שהסברתי)
genai.configure(api_key=os.environ.get("GEMINI_API_KEY"))

@app.route('/ask_ai', methods=['GET', 'POST'])
def ask_ai():
    # קבלת נתיב הקובץ שהוקלט
    audio_path = request.values.get('path')
    yemot_token = os.environ.get("YEMOT_TOKEN")

    # אם זו תחילת שיחה ואין עדיין הקלטה
    if not audio_path:
        # פקודה להקלטה (f-800 זה "נא להקליט לאחר הצפצוף")
        return "read=f-800&target=path&max=20&beep=yes"

    try:
        # 1. הורדת הקובץ מימות המשיח לשרת הזמני של Render
        download_url = f"https://www.call2all.co.il/ym/api/DownloadFile?token={yemot_token}&path={audio_path}"
        response = requests.get(download_url)
        
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_file:
            tmp_file.write(response.content)
            tmp_path = tmp_file.name

        # 2. העלאת הקובץ ל-Gemini
        model = genai.GenerativeModel("gemini-1.5-flash")
        sample_file = genai.upload_file(path=tmp_path, mime_type="audio/wav")
        
        # 3. בקשת תשובה
        result = model.generate_content([
            "תענה על השאלה בקובץ השמע בעברית קצרה מאוד ולעניין. אל תשתמש בסימנים מיוחדים.",
            sample_file
        ])
        
        # ניקוי התשובה מסימנים שמשבשים את הקריין
        clean_text = "".join(c for c in result.text if c.isalnum() or c in " .")
        
        return f"id_list_message=t-{clean_text}"

    except Exception as e:
        print(f"Error: {e}")
        return "id_list_message=t-חלה שגיאה בעיבוד השמע"

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)

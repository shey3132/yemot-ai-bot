from flask import Flask, request
import os
import requests
import google.generativeai as genai
import tempfile

app = Flask(__name__)

# הגדרת מפתח Gemini
GOOGLE_API_KEY = os.environ.get("GEMINI_API_KEY")
genai.configure(api_key=GOOGLE_API_KEY)

# הגדרת טוקן ימות המשיח (חובה כדי להוריד את קובץ ההקלטה)
YEMOT_TOKEN = os.environ.get("YEMOT_TOKEN")

@app.route('/ask_ai', methods=['GET', 'POST'])
def ask_ai():
    # 1. בדיקה: האם יש לנו מפתחות מוגדרים?
    if not GOOGLE_API_KEY:
        return "id_list_message=t-שגיאה, חסר מפתח ג'מימני בהגדרות השרת"
    if not YEMOT_TOKEN:
        print("Warning: YEMOT_TOKEN is missing. Download might fail.")

    # 2. קבלת נתונים מימות המשיח
    # הפרמטר 'path' מכיל את נתיב ההקלטה בימות המשיח (למשל: EnterID/...)
    audio_path = request.values.get('path')

    # --- תרחיש א': אין הקלטה (כניסה ראשונה לשיחה) ---
    if not audio_path:
        # אנו שולחים פקודת read: "תשמיע, תצפצף, תקליט, ותחזור לפה עם המשתנה path"
        return "read=t-נא לשאול את השאלה לאחר הצפצוף, ובסיום להקיש סולמית&target=path&max=60&beep=yes"

    # --- תרחיש ב': יש הקלטה (חזרנו מהקלטה) ---
    print(f"Recording received at path: {audio_path}")
    
    temp_file_path = None
    try:
        # א. הורדת קובץ השמע מימות המשיח
        # כתובת ה-API להורדת קבצים
        download_url = f"https://www.call2all.co.il/ym/api/DownloadFile?token={YEMOT_TOKEN}&path={audio_path}"
        
        response = requests.get(download_url)
        
        if response.status_code != 200:
            print(f"Error downloading file: {response.text}")
            return "id_list_message=t-חלה שגיאה בהורדת ההקלטה מהמערכת"

        # ב. שמירת הקובץ זמנית בשרת של Render
        # אנו יוצרים קובץ זמני עם סיומת wav
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp_file:
            temp_file.write(response.content)
            temp_file_path = temp_file.name

        # ג. שליחה ל-Gemini
        print("Uploading file to Gemini...")
        gemini_file = genai.upload_file(temp_file_path, mime_type="audio/wav")
        
        # בחירת המודל (Flash מהיר וטוב לאודיו)
        model = genai.GenerativeModel("gemini-1.5-flash")
        
        print("Generating content...")
        # הבקשה ל-AI: הקשב להקלטה וענה בעברית
        result = model.generate_content([
            "Listen to this audio recording (which is in Hebrew). Understand the user's question and provide a helpful, concise answer in Hebrew.",
            gemini_file
        ])
        
        ai_response_text = result.text
        
        # ניקוי: הסרת כוכביות או סימנים שלא נשמעים טוב ב-TTS
        clean_response = ai_response_text.replace("*", "").replace("\n", ". ")
        
        print(f"AI Response: {clean_response}")

        # ד. החזרת התשובה לימות המשיח
        return f"id_list_message=t-{clean_response}"

    except Exception as e:
        print(f"CRITICAL ERROR: {e}")
        return "id_list_message=t-חלה שגיאה במערכת הבינה המלאכותית"
        
    finally:
        # ה. ניקוי הקובץ הזמני מהשרת
        if temp_file_path and os.path.exists(temp_file_path):
            os.remove(temp_file_path)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)

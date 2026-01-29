from flask import Flask, request
import requests
import google.generativeai as genai
import os

app = Flask(__name__)

# הגדרת מפתח ה-API של Gemini - ודא שהגדרת אותו ב-Render ב-Environment Variables
genai.configure(api_key=os.environ.get("GEMINI_API_KEY"))

@app.route('/ask_ai', methods=['GET', 'POST'])
def ask_ai():
    # 1. שליפת הפרמטרים מימות המשיח (כפי שראינו ב-ApiSend בלוג)
    audio_path = request.values.get('path')
    token = request.values.get('ApiCallId') # מזהה שיחה לצרכי לוגים

    # 2. שלב א: אם אין נתיב הקלטה, נבקש מימות המשיח להקליט
    if not audio_path:
        # פקודת read גורמת למערכת להשמיע הודעה, לצפצף ולהמתין להקלטה
        # הפרמטר target=path אומר שההקלטה תחזור לשרת תחת המשתנה path
        return "read=t-נא להגיד את שאלתכם לאחר הצפצוף ובסיום להקיש סולמית&target=path&max=30&beep=yes"

    # 3. שלב ב: יש הקלטה! ננסה לעבד אותה
    try:
        # בניית הקישור להורדת הקובץ מימות המשיח
        # נניח שהקובץ הוא בפורמט wav כפי שמוגדר במערכת
        file_url = f"https://www.call2all.co.il/ym/api/DownloadFile?path={audio_path}"
        
        # כאן אתה אמור להוסיף את הלוגיקה של Gemini:
        # א. הורדת הקובץ מה-URL
        # ב. שליחתו ל-Gemini לתרגום (Speech-to-Text) ומענה
        
        # לצורך בדיקה ראשונית, נחזיר הודעה שהקובץ התקבל:
        return f"id_list_message=t-ההקלטה התקבלה בהצלחה בנתיב {audio_path}. מעבד את פנייתך..."

    except Exception as e:
        # במקרה של שגיאה בשרת, נחזיר הודעה מסודרת לימות המשיח
        return "id_list_message=t-חלה שגיאה בתקשורת עם שרת ה-AI"

if __name__ == '__main__':
    # הרצת השרת על הפורט ש-Render נותן
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)

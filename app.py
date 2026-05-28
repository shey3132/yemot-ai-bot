import os
import tempfile
import requests
from flask import Flask, request, Response
from google import genai
from google.genai import types

app = Flask(__name__)

# הגדרות סביבה מ-Render
YEMOT_TOKEN = os.environ.get("YEMOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# אתחול הלקוח הרשמי והחדש של גוגל
client = genai.Client(api_key=GEMINI_API_KEY)

@app.route('/ai-chat', methods=['GET', 'POST'])
def ai_chat():
    data = request.form if request.method == 'POST' else request.args
    
    # הגנה מפני בקשות ניתוק (למניעת לולאות שרת מיותרות)
    if data.get('hangup') == 'yes':
        return Response("noop", mimetype='text/plain')

    # קריאת הפרמטר המקורי שלך
    audio_path = data.get('user_audio')

    # פנייה ראשונית - החזרת מחרוזת ה-read המקורית והמדויקת שלך שעבדה!
    if not audio_path:
        return Response(
            "read=t-שלום, אני מאזין. במה אוכל לעזור?=user_audio,no,record,,,,,15,,", 
            mimetype='text/plain'
        )

    # --- טיפול מדויק בנתיב הקובץ לפי ההנחיות שלך ---
    # מוודאים שיש סלאש מוביל לפני נתיב הקובץ (למשל: /1/079.wav)
    clean_path = audio_path if audio_path.startswith('/') else '/' + audio_path
    # מוסיפים את הקידומת ivr2: כנדרש
    yemot_path = f"ivr2:{clean_path}"

    # הורדת קובץ השמע המוקלט
    # הערה: ספריית requests משרשרת ומבצעת URL Encode אוטומטי ומלא לפרמטרים בתוך params
    params = {"token": YEMOT_TOKEN, "path": yemot_path}
    try:
        audio_response = requests.get("https://www.call2all.co.il/ym/api/DownloadFile", params=params)
        audio_response.raise_for_status()
        
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_file:
            tmp_file.write(audio_response.content)
            tmp_filename = tmp_file.name
    except Exception as e:
        print(f"Error downloading audio: {e}")
        return Response("read=t-חלה שגיאה זמנית בהורדת השמע. נסה שוב.=user_audio,no,record,,,,,15,,", mimetype='text/plain')

    try:
        # העלאת הקובץ לשרתי גוגל
        audio_file = client.files.upload(file=tmp_filename)
        
        # קריאה למודל gemini-2.5-flash עם מנוע החיפוש המובנה של גוגל
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=[
                "אתה עוזר קולי חכם בטלפון. ענה בקיצור נמרץ מאוד (עד 2 משפטים), ללא סימני פיסוק מיוחדים, ללא כוכביות, וללא רשימות. תן תשובה חלקה שמתאימה להקראה קולית ישירה.",
                audio_file
            ],
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())]
            )
        )
        ai_reply = response.text or "לא הצלחתי לעבד את התשובה, אנא נסה שוב."
        
        # ניקוי מחמיר של תווים שמורים כדי שלא ישבשו את הפארסר של ימות המשיח
        clean_reply = ai_reply.replace("**", "").replace("*", "").replace("#", "")
        clean_reply = clean_reply.replace("&", " ו- ").replace("=", " פירושו ")
        clean_reply = " ".join(clean_reply.split())
        
        # החזרת התשובה והמשך לולאת ההקלטה באותו פורמט בדיוק
        return Response(
            f"read=t-{clean_reply}=user_audio,no,record,,,,,15,,", 
            mimetype='text/plain'
        )

    except Exception as e:
        print(f"Error during AI processing: {e}")
        return Response("read=t-מתנצל, קרתה תקלה בעיבוד הנתונים. נסה שוב.=user_audio,no,record,,,,,15,,", mimetype='text/plain')
    
    finally:
        # ניקוי קבצים מקומיים ומשרתי גוגל למניעת עומס בשטח הדיסק
        if 'tmp_filename' in locals() and os.path.exists(tmp_filename): 
            os.remove(tmp_filename)
        if 'audio_file' in locals():
            try: client.files.delete(name=audio_file.name)
            except: pass

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)

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

# אתחול הלקוח הרשמי של גוגל (SDK החדש)
client = genai.Client(api_key=GEMINI_API_KEY)

# מחרוזת הפרמטרים המדויקת של ההקלטה - ללא שום שינוי
RECORD_COMMAND = "user_audio,no,record,,,yes,yes,no,1,60"

@app.route('/ai-chat', methods=['GET', 'POST'])
def ai_chat():
    data = request.form if request.method == 'POST' else request.args
    
    # חסימת בקשות ניתוק כדי למנוע לולאות
    if data.get('hangup') == 'yes':
        return Response("noop", mimetype='text/plain')

    # קריאת הפרמטר שבו ימות המשיח מחזירה את נתיב הקובץ
    audio_path = data.get('user_audio')

    # פנייה ראשונית - **טקסט נקי לחלוטין ללא פסיקים או נקודות!**
    if not audio_path:
        return Response(
            f"read=t-שלום אני מאזין במה אוכל לעזור={RECORD_COMMAND}", 
            mimetype='text/plain'
        )

    # הוספת הקידומת ivr2: לנתיב
    yemot_path = f"ivr2:{audio_path}"

    params = {"token": YEMOT_TOKEN, "path": yemot_path}
    try:
        audio_response = requests.get("https://www.call2all.co.il/ym/api/DownloadFile", params=params)
        audio_response.raise_for_status()
        
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_file:
            tmp_file.write(audio_response.content)
            tmp_filename = tmp_file.name
    except Exception as e:
        print(f"Error downloading audio: {e}")
        return Response(f"read=t-חלה שגיאה בקבלת השמע אנא נסו שוב={RECORD_COMMAND}", mimetype='text/plain')

    try:
        audio_file = client.files.upload(file=tmp_filename)
        
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=[
                "אתה עוזר קולי חכם בטלפון. ענה בקיצור נמרץ מאוד (עד 2 משפטים). אל תשתמש בשום סימני פיסוק - ללא פסיקים, ללא נקודות, וללא סימני שאלה. תן תשובה חלקה למנוע הקראה.",
                audio_file
            ],
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())]
            )
        )
        ai_reply = response.text or "לא הצלחתי לעבד את התשובה אנא נסה שוב"
        
        # --- ניקוי אגרסיבי: חובה להסיר כל סימן שיכול לשבור את הפארסר ---
        clean_reply = ai_reply.replace("**", "").replace("*", "").replace("#", "")
        # מסירים פסיקים, נקודות, סימני שאלה וקריאה
        clean_reply = clean_reply.replace(",", "").replace(".", "").replace("?", "").replace("!", "").replace(":", "").replace("-", " ")
        clean_reply = clean_reply.replace("&", " ו ").replace("=", " ")
        clean_reply = " ".join(clean_reply.split()) # מוריד רווחים כפולים
        
        # החזרת התשובה הנקייה
        return Response(
            f"read=t-{clean_reply}={RECORD_COMMAND}", 
            mimetype='text/plain'
        )

    except Exception as e:
        print(f"Error during AI processing: {e}")
        return Response(f"read=t-קרתה תקלה בעיבוד הנתונים נסה שוב={RECORD_COMMAND}", mimetype='text/plain')
    
    finally:
        if 'tmp_filename' in locals() and os.path.exists(tmp_filename): 
            os.remove(tmp_filename)
        if 'audio_file' in locals():
            try: client.files.delete(name=audio_file.name)
            except: pass

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)

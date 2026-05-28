import os
import time
import tempfile
import requests
from flask import Flask, request, Response
from google import genai
from google.genai import types

app = Flask(__name__)

YEMOT_TOKEN = os.environ.get("YEMOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

client = genai.Client(api_key=GEMINI_API_KEY)
RECORD_COMMAND = "user_audio,no,record,,,yes,yes,no,1,60"

@app.route('/ai-chat', methods=['GET', 'POST'])
def ai_chat():
    data = request.form if request.method == 'POST' else request.args
    
    if data.get('hangup') == 'yes':
        return Response("noop", mimetype='text/plain')

    audio_path = data.get('user_audio')

    # שלב א': כניסה ראשונית ללא קובץ
    if not audio_path:
        return Response(
            f"read=t-שלום אני מאזין במה אוכל לעזור={RECORD_COMMAND}", 
            mimetype='text/plain'
        )

    # שלב ב': הורדת הקובץ מימות המשיח
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

    # שלב ג': שליחה לגוגל עם מנגנון ניסיון חוזר (Retry) לעקיפת שגיאות 503
    try:
        audio_file = client.files.upload(file=tmp_filename)
        
        max_retries = 3
        ai_reply = None
        
        for attempt in range(max_retries):
            try:
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
                ai_reply = response.text
                break # יציאה מהלולאה אם הבקשה הצליחה
            except Exception as e:
                print(f"Google API attempt {attempt + 1} failed: {e}")
                if attempt < max_retries - 1:
                    time.sleep(2) # המתנה של 2 שניות לפני הניסיון הבא
                else:
                    raise e # אם כל הניסיונות נכשלו, נזרוק את השגיאה החוצה
        
        if not ai_reply:
            ai_reply = "לא הצלחתי לעבד את התשובה אנא נסה שוב"
            
        # ניקוי הטקסט
        clean_reply = ai_reply.replace("**", "").replace("*", "").replace("#", "")
        clean_reply = clean_reply.replace(",", "").replace(".", "").replace("?", "").replace("!", "").replace(":", "").replace("-", " ")
        clean_reply = clean_reply.replace("&", " ו ").replace("=", " ")
        clean_reply = " ".join(clean_reply.split())
        
        return Response(
            f"read=t-{clean_reply}={RECORD_COMMAND}", 
            mimetype='text/plain'
        )

    except Exception as e:
        print(f"Error during AI processing: {e}")
        # הודעה מותאמת אישית אם השרתים של גוגל עמוסים לגמרי
        return Response(f"read=t-העוזר החכם עמוס כרגע אנא נסה שוב בעוד מספר שניות={RECORD_COMMAND}", mimetype='text/plain')
    
    finally:
        if 'tmp_filename' in locals() and os.path.exists(tmp_filename): 
            os.remove(tmp_filename)
        if 'audio_file' in locals():
            try: client.files.delete(name=audio_file.name)
            except: pass

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)

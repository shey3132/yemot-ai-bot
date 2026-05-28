import os
import tempfile
import requests
from flask import Flask, request, Response
from google import genai
from google.genai import types

app = Flask(__name__)

# הגדרות סביבה מה-Render
YEMOT_TOKEN = os.environ.get("YEMOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# אתחול הלקוח החדש של גוגל (SDK 2026 המעודכן)
client = genai.Client(api_key=GEMINI_API_KEY)

@app.route('/ai-chat', methods=['GET', 'POST'])
def ai_chat():
    # קבלת הנתונים בין אם זה GET או POST
    data = request.form if request.method == 'POST' else request.args
    audio_path = data.get('user_audio')

    # פנייה ראשונית (התחלת השיחה) - המערכת מברכת ומפעילה את המקליט
    if not audio_path:
        return Response(
            "read=t-שלום, אני מאזין. במה אוכל לעזור?=user_audio,no,record,,,,,15,,", 
            mimetype='text/plain'
        )

    # הורדת קובץ השמע המוקלט משרתי ימות המשיח
    params = {"token": YEMOT_TOKEN, "path": f"ivr2:{audio_path}"}
    try:
        audio_response = requests.get("https://www.call2all.co.il/ym/api/DownloadFile", params=params)
        audio_response.raise_for_status()
        
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_file:
            tmp_file.write(audio_response.content)
            tmp_filename = tmp_file.name
    except Exception as e:
        print(f"Error downloading audio: {e}")
        return Response("read=t-חלה שגיאה זמנית בהורדת קובץ השמע. אנא נסה שוב.=user_audio,no,record,,,,,15,,", mimetype='text/plain')

    try:
        # העלאת הקובץ לשרת ה-File API של גוגל
        audio_file = client.files.upload(file=tmp_filename)
        
        # קריאה למודל החדש ביותר עם הפעלת מנוע החיפוש (Google Search Grounding)
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=[
                "אתה עוזר קולי חכם בטלפון. ענה בקיצור נמרץ (עד 2 משפטים), ללא סימני פיסוק מיוחדים, ללא כוכביות, וללא רשימות. תן תשובה חלקה שמתאימה להקראה קולית ישירה.",
                audio_file
            ],
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())]
            )
        )
        ai_reply = response.text or "לא הצלחתי לעבד את התשובה, אנא נסה שוב."
        
        # --- ניקוי מחמיר של הטקסט להתאמה מלאה ל-Plain Text של ימות המשיח ---
        # הסרת כוכביות של הדגשות (Markdown) וסימני כותרות שלא יוקראו כטקסט
        clean_reply = ai_reply.replace("**", "").replace("*", "").replace("#", "")
        # החלפת תווים שמורים של ה-API כדי שלא ישבשו את מבנה הפקודות (פותר את בעיית ה-& וה-=)
        clean_reply = clean_reply.replace("&", " ו- ").replace("=", " פירושו ")
        # הסרת ירידות שורה ורווחים כפולים מיותרים
        clean_reply = " ".join(clean_reply.split())
        
        # החזרת הפקודה read במבנה טקסט נקי לחלוטין
        return Response(
            f"read=t-{clean_reply}=user_audio,no,record,,,,,15,,", 
            mimetype='text/plain'
        )

    except Exception as e:
        print(f"Error during AI processing: {e}")
        return Response("read=t-מתנצל, קרתה תקלה בעיבוד הנתונים. נסה שוב.=user_audio,no,record,,,,,15,,", mimetype='text/plain')
    
    finally:
        # ניקוי קבצים זמניים מקומיים ומשרתי גוגל למניעת עומס שטח
        if 'tmp_filename' in locals() and os.path.exists(tmp_filename): 
            os.remove(tmp_filename)
        if 'audio_file' in locals():
            try:
                client.files.delete(name=audio_file.name)
            except:
                pass

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)

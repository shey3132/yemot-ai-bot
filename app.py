import os
import tempfile
import requests
from flask import Flask, request, Response
import google.generativeai as genai

app = Flask(__name__)

# משיכת מפתחות
YEMOT_TOKEN = os.environ.get("YEMOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

genai.configure(api_key=GEMINI_API_KEY)

# טעינת המודל + הפעלת חיפוש בזמן אמת בגוגל (Google Search Grounding)
model = genai.GenerativeModel(
    'gemini-2.5-flash-lite',
    tools=[{"google_search_retrieval": {}}]
)

sessions = {}

@app.route('/ai-chat', methods=['GET', 'POST'])
def ai_chat():
    data = request.form if request.method == 'POST' else request.args
    user_phone = data.get('ApiPhone')
    user_id = user_phone if user_phone else data.get('ApiCallId', 'unknown')
    audio_path = data.get('user_audio')

    if not audio_path:
        if user_id not in sessions:
            system_prompt = "אתה עוזר קולי חכם המחובר לאינטרנט. ענה תשובות זורמות, עדכניות ובגובה העיניים."
            sessions[user_id] = [{"role": "user", "parts": [system_prompt]}, {"role": "model", "parts": ["שלום, אני מחובר ומוכן לעזור."]}]
        
        # פקודת read חוקית ונקייה להתחלת השיחה
        return Response("read=t-שלום אני מוכן אנא דבר=user_audio,no,record,,,,,15,,", mimetype='text/plain')

    # הורדת הקובץ מימות המשיח
    params = {"token": YEMOT_TOKEN, "path": f"ivr2:{audio_path}"}
    audio_response = requests.get("https://www.call2all.co.il/ym/api/DownloadFile", params=params)
    
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_file:
        tmp_file.write(audio_response.content)
        tmp_filename = tmp_file.name

    try:
        # העלאת האודיו לגוגל באופן זמני
        audio_file = genai.upload_file(path=tmp_filename)
        if user_id not in sessions: sessions[user_id] = []
        
        # יצירת הקשר זמני (היסטוריה + אודיו נוכחי) - חוסך את המכסות לחלוטין!
        current_chat = sessions[user_id] + [{"role": "user", "parts": [audio_file]}]
        
        response = model.generate_content(current_chat)
        ai_reply = response.text
        
        # שמירת היסטוריה כטקסט בלבד
        sessions[user_id].append({"role": "user", "parts": ["(המשתמש דיבר)"]})
        sessions[user_id].append({"role": "model", "parts": [ai_reply]})
        
        # הגבלת ההיסטוריה ל-6 הודעות כדי שהשרת של Render לא יקרוס מזיכרון
        if len(sessions[user_id]) > 6:
            sessions[user_id] = sessions[user_id][-6:]
        
        # ---------------------------------------------------------
        # ניקוי אגרסיבי במיוחד של הטקסט עבור ימות המשיח
        # ---------------------------------------------------------
        # 1. הסרת תווים ששוברים את ה-URL
        clean_reply = ai_reply.replace("&", " ו").replace("=", " שווה ").replace("*", "").replace("#", "")
        clean_reply = clean_reply.replace(",", " ").replace("-", " ")
        clean_reply = clean_reply.replace('"', '').replace("'", "")
        # 2. השטחה מוחלטת לשורה אחת (מוחק Enters ורווחים כפולים)
        clean_reply = " ".join(clean_reply.split())
        
        # יצירת התגובה במבנה read מדויק
        api_response = f"read=t-{clean_reply}=user_audio,no,record,,,,,15,,"
        
        return Response(api_response, mimetype='text/plain')

    except Exception as e:
        print(f"DEBUG Error: {e}")
        return Response("read=t-קרתה תקלה, נסה שוב=user_audio,no,record,,,,,15,,", mimetype='text/plain')
    
    finally:
        # ניקוי קבצים מהשרת שלך
        if os.path.exists(tmp_filename): os.remove(tmp_filename)
        # ניקוי הקובץ מהשרתים של גוגל
        try:
            if 'audio_file' in locals():
                genai.delete_file(audio_file.name)
        except Exception:
            pass

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)

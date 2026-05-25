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
# שימוש במודל החסכוני והמהיר יותר
model = genai.GenerativeModel('gemini-2.5-flash-lite')

sessions = {}

@app.route('/ai-chat', methods=['GET', 'POST'])
def ai_chat():
    data = request.form if request.method == 'POST' else request.args
    user_phone = data.get('ApiPhone')
    user_id = user_phone if user_phone else data.get('ApiCallId', 'unknown')
    audio_path = data.get('user_audio')

    if not audio_path:
        if user_id not in sessions:
            # פתרון ל-2 הבעיות: הגדרת השנה ל-2026, ושינוי ההוראה לתשובה מפורטת וטבעית
            system_prompt = "אנחנו בשנת 2026. אתה עוזר קולי חכם שעונה בשיחה קולית. ענה בצורה מפורטת, טבעית, שירותית ובגובה העיניים."
            sessions[user_id] = [{"role": "user", "parts": [system_prompt]}, {"role": "model", "parts": ["הבנתי, אני מוכן לעזור."]}]
        
        return Response("read=t-שלום אני מוכן אנא דבר=user_audio,no,record,,,,,15,,", mimetype='text/plain')

    params = {"token": YEMOT_TOKEN, "path": f"ivr2:{audio_path}"}
    audio_response = requests.get("https://www.call2all.co.il/ym/api/DownloadFile", params=params)
    
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_file:
        tmp_file.write(audio_response.content)
        tmp_filename = tmp_file.name

    try:
        # העלאת האודיו לגוגל
        audio_file = genai.upload_file(path=tmp_filename)
        if user_id not in sessions: sessions[user_id] = []
        
        # פתרון המכסות: יוצרים רשימה זמנית רק לשאלה הזו, שכוללת את ההיסטוריה + האודיו הנוכחי
        current_chat = sessions[user_id] + [{"role": "user", "parts": [audio_file]}]
        
        # מקבלים תשובה מג'מיני
        response = model.generate_content(current_chat)
        ai_reply = response.text
        
        # שומרים להיסטוריה *רק טקסט* כדי לא לפוצץ את הזיכרון והמכסות של גוגל בבקשות הבאות
        sessions[user_id].append({"role": "user", "parts": ["(המשתמש שלח הודעה קולית והיא נענתה)"]})
        sessions[user_id].append({"role": "model", "parts": [ai_reply]})
        
        # הגבלת היסטוריה: שומרים רק את 8 ההודעות האחרונות כדי שהשיחה תרוץ לנצח בלי לחרוג מהמכסה
        if len(sessions[user_id]) > 8:
            sessions[user_id] = sessions[user_id][-8:]
        
        # ניקוי תווים
        clean_reply = ai_reply.replace("&", " ו").replace("=", " שווה ").replace("*", "").replace("#", "")
        clean_reply = clean_reply.replace(",", " ").replace("-", " ")
        clean_reply = clean_reply.replace("\n", " ").replace("\r", " ")
        
        api_response = f"read=t-{clean_reply}=user_audio,no,record,,,,,15,,"
        return Response(api_response, mimetype='text/plain')

    except Exception as e:
        print(f"DEBUG: Error: {e}")
        return Response("read=t-קרתה תקלה נסה שוב=user_audio,no,record,,,,,15,,", mimetype='text/plain')
    
    finally:
        # מחיקת הקובץ הזמני מהשרת שלך
        if os.path.exists(tmp_filename): os.remove(tmp_filename)
        # מחיקת הקובץ מהשרתים של גוגל כדי לחסוך לך שטח אחסון במשתמש החינמי!
        try:
            if 'audio_file' in locals():
                genai.delete_file(audio_file.name)
        except Exception:
            pass

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)

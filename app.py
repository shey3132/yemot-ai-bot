import os
import tempfile
import requests
from flask import Flask, request
import google.generativeai as genai

app = Flask(__name__)

# ==========================================
# הגדרות מערכת ומפתחות (הכנס את הנתונים שלך)
# ==========================================
YEMOT_TOKEN = "1234567:YOUR_YEMOT_PASSWORD" # מספר מערכת וסיסמה או API Key של ימות המשיח
GEMINI_API_KEY = "YOUR_GEMINI_API_KEY"      # המפתח שקיבלת מ-Google AI Studio

# הגדרת הגישה ל-API של ג'מיני
genai.configure(api_key=GEMINI_API_KEY)

# אתחול מודל ג'מיני. אנחנו משתמשים ב-Flash כי הוא מהיר ותומך בעיבוד אודיו ישיר
model = genai.GenerativeModel('gemini-1.5-flash')

# מסד נתונים זמני (בזיכרון השרת) לשמירת היסטוריית השיחות לפי ApiCallId
# זה מבטיח שהבוט יזכור את ההקשר מתחילת השיחה ועד סופה
sessions = {}

@app.route('/ai-chat', methods=['GET', 'POST'])
def ai_chat():
    # משיכת הנתונים שימות המשיח שולחת (תומך גם ב-GET וגם ב-POST)
    data = request.form if request.method == 'POST' else request.args
    
    call_id = data.get('ApiCallId')
    audio_path = data.get('user_audio') # נתיב ההקלטה, יתקבל רק אחרי שהמאזין דיבר

    # ==========================================
    # מצב 1: תחילת שיחה - עדיין אין הקלטה
    # ==========================================
    if not audio_path:
        # אתחול היסטוריית השיחה של המשתמש הנוכחי
        # אנחנו נותנים למודל הוראת מערכת ראשונית (System Prompt)
        sessions[call_id] = [
            {"role": "user", "parts": ["מעכשיו אתה עוזר קולי בעברית דרך הטלפון. עליך לענות תשובות ענייניות, קצרות וברורות. אסור לך להשתמש באמוג'י, סימנים מתמטיים או כוכביות, כי המערכת תקריא אותם בקול וזה יישמע רע. הבנת?"]},
            {"role": "model", "parts": ["הבנתי. אני מוכן לעזור."]}
        ]
        
        # מחזירים לימות המשיח פקודה להשמיע פתיח ולהקליט את המאזין
        response_text = "id_list_message=t-שלום, אני מודל ג'מיני, איך אוכל לעזור היום?&"
        response_text += "read=t-אנא דבר אחרי הצפצוף=user_audio,no,record,,,,,15,,"
        return response_text

    # ==========================================
    # מצב 2: קבלת הקלטה ועיבוד מול הבינה המלאכותית
    # ==========================================
    
    # 1. הורדת קובץ ההקלטה מהשרתים של ימות המשיח
    download_url = f"https://www.call2all.co.il/ym/api/DownloadFile?token={YEMOT_TOKEN}&path={audio_path}"
    audio_response = requests.get(download_url)
    
    if audio_response.status_code != 200:
        return "id_list_message=t-הייתה תקלה בקבלת ההקלטה, אנא נסה שוב&read=t-אנא דבר אחרי הצפצוף=user_audio,no,record,,,,,15,,"

    # 2. שמירת הקובץ זמנית בשרת כדי שנוכל לשלוח אותו לג'מיני
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_file:
        tmp_file.write(audio_response.content)
        tmp_filename = tmp_file.name

    # וידוא שקיים סשן (למקרה שהשרת עשה ריסטרט באמצע השיחה)
    if call_id not in sessions:
        sessions[call_id] = [
            {"role": "user", "parts": ["אתה עוזר קולי טלפוני. ענה קצר וללא סימנים מיוחדים."]},
            {"role": "model", "parts": ["בסדר."]}
        ]

    try:
        # 3. העלאת קובץ השמע ישירות לגוגל (File API)
        audio_file = genai.upload_file(path=tmp_filename)
        
        # 4. הוספת האודיו כחלק מהיסטוריית השיחה הרציפה
        sessions[call_id].append({"role": "user", "parts": [audio_file]})
        
        # 5. שליחת היסטוריית השיחה כולה (כולל ההקלטה החדשה) למודל להפקת תגובה
        response = model.generate_content(sessions[call_id])
        ai_reply = response.text
        
        # 6. הוספת התשובה של המודל להיסטוריה כדי שיזכור אותה בפנייה הבאה
        sessions[call_id].append({"role": "model", "parts": [ai_reply]})
        
        # 7. מחיקת הקובץ מהשרת של גוגל לאחר השימוש (שומר על סדר ומונע חריגות אחסון)
        audio_file.delete()
        
    except Exception as e:
        print(f"Error calling Gemini API: {e}")
        ai_reply = "מצטער, חלה שגיאה בעיבוד הקול מול שרת הבינה המלאכותית."

    # מחיקת הקובץ הזמני מהשרת שלך
    if os.path.exists(tmp_filename):
        os.path.remove(tmp_filename)

    # ==========================================
    # מצב 3: החזרת התשובה לטלפון
    # ==========================================
    
    # ניקוי התשובה מתווים ששוברים את ה-API של ימות המשיח (כמו & או =) 
    # או מפריעים להקראה הקולית (כמו כוכביות של מודגש)
    clean_reply = ai_reply.replace("&", " ו").replace("=", " שווה ").replace("*", "").replace("#", "")
    
    # בניית שרשור הפקודות: הקראת התשובה + פתיחת הקלטה חדשה להמשך השיחה
    response_text = f"id_list_message=t-{clean_reply}&"
    response_text += "read=t-המשך לדבר אחרי הצפצוף=user_audio,no,record,,,,,15,,"

    return response_text

if __name__ == '__main__':
    # הפעלת השרת על הפורט הדיפולטיבי (ניתן לשנות לפי דרישות האחסון שלך)
    app.run(host='0.0.0.0', port=5000)

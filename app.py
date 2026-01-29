from flask import Flask, request
import os
import requests
import tempfile
import logging
import re
import traceback
import google.generativeai as genai

app = Flask(__name__)

# לוגינג בסיסי
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# הגדרת Gemini (וודא שהמשתנה ENV הגדרתי)
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")
if GEMINI_KEY:
    genai.configure(api_key=GEMINI_KEY)
else:
    logger.warning("לא נמצא GEMINI_API_KEY בסביבת הריצה; קריאות ל-Gemini ייכשלו אם ינסו להתבצע.")

@app.route('/ask_ai', methods=['GET', 'POST'])
def ask_ai():
    # קבלת הנתונים מימות המשיח
    audio_path = request.values.get('path')
    token = os.environ.get("YEMOT_TOKEN")

    # אם עדיין לא הקליטו - פקודת הקלטה קצרה (כמו שהיה)
    if not audio_path:
        return "read=f-800&target=path&max=20&beep=yes"

    # בדיקות מקדימות
    if not token:
        logger.error("YEMOT_TOKEN לא מוגדר בסביבה.")
        return "id_list_message=t-חלה תקלה טכנית"

    # הורדת הקובץ מהשירות
    file_url = f"https://www.call2all.co.il/ym/api/DownloadFile?token={token}&path={audio_path}"
    tf_path = None

    try:
        r = requests.get(file_url, timeout=15)
        if r.status_code != 200:
            logger.error("שגיאה בהורדת הקובץ: HTTP %s", r.status_code)
            return "id_list_message=t-חלה תקלה טכנית"

        # שמירת הקובץ הזמני
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
            tf.write(r.content)
            tf.flush()
            tf_path = tf.name

        # --- אינטראקציה עם Gemini ---
        # שימו לב: שימוש ב-API של google.generativeai יכול להשתנות לפי גרסה.
        # הקוד מנסה לבצע קריאה כפי שביקשת, אך עוטף את זה במטפל שגיאות ברור.
        if not GEMINI_KEY:
            logger.error("אין מפתח Gemini; לא ניתן לבצע המרה/שיחה ל-Gemini.")
            return "id_list_message=t-חלה תקלה טכנית"

        try:
            model = genai.GenerativeModel("gemini-1.5-flash")
            # העלאת הקובץ (תלוי ב־SDK)
            uploaded = genai.upload_file(path=tf_path)

            # קריאת המודל — השארתי את המבנה דומה למקור שלך אך עם בדיקות
            try:
                response = model.generate_content(["תענה בקצרה בעברית", uploaded])
            except Exception as inner_e:
                # אם ה-SDK שונה, יתכן שהתשובה תהיה במבנה שונה — קח את מה שיש
                logger.warning("שגיאה ב-calling generate_content: %s", inner_e)
                # ניסיון גיבוי לקריאה אחרת (תלוי בגרסה)
                response = model.generate(["תענה בקצרה בעברית", uploaded])

            # הבאת הטקסט מתוך התשובה בדרכים שונות
            text = None
            if hasattr(response, "text"):
                text = response.text
            elif isinstance(response, dict):
                text = response.get("text") or response.get("output") or response.get("content")
            else:
                # ניסיון המרה ל-string
                text = str(response)

            if not text:
                logger.error("לא התקבלה תשובה טקסטואלית מ-Gemini.")
                return "id_list_message=t-חלה תקלה טכנית"

            # ניקוי תשובה — שומר על עברית, אנגלית, ספרות ופיסוק פשוט (.,-)
            # ממיר תווים לא רצויים לריק
            clean = re.sub(r"[^\w\u0590-\u05FF\s\.\,\-]", "", text, flags=re.UNICODE).strip()

            # אם אחרי הניקוי אין תוכן — החזר שגיאה ידידותית
            if not clean:
                logger.warning("תשובה ריקה אחרי ניקוי.")
                return "id_list_message=t-חלה תקלה טכנית"

            # החזר בפורמט המבוקש
            return f"id_list_message=t-{clean}"

        except Exception as gen_e:
            logger.error("שגיאה בעת קריאה ל-Gemini: %s\n%s", gen_e, traceback.format_exc())
            return "id_list_message=t-חלה תקלה טכנית"

    except requests.RequestException as req_e:
        logger.error("שגיאת רשת בהורדת הקובץ: %s", req_e)
        return "id_list_message=t-חלה תקלה טכנית"

    except Exception as e:
        logger.error("שגיאה לא צפויה: %s\n%s", e, traceback.format_exc())
        return "id_list_message=t-חלה תקלה טכנית"

    finally:
        # ניקוי הקובץ הזמני אם נוצר
        try:
            if tf_path and os.path.exists(tf_path):
                os.remove(tf_path)
        except Exception as rm_e:
            logger.warning("לא הצלחנו למחוק קובץ טמפר: %s", rm_e)


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)

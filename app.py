from flask import Flask, request
import os, requests, tempfile, logging, re, traceback
from google import genai

# הגדרות לוגינג
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("yemot-ai")

app = Flask(__name__)

def get_gemini_client():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        logger.error("❌ GEMINI_API_KEY חסר בהגדרות השרת")
        return None
    try:
        return genai.Client(api_key=api_key)
    except Exception as e:
        logger.error(f"❌ שגיאה בחיבור ל-Gemini: {e}")
        return None

@app.route("/ask_ai", methods=["GET", "POST"])
def ask_ai():
    # קבלת כל הפרמטרים מימות המשיח
    params = request.values.to_dict()
    logger.info(f"📥 בקשה נכנסת: {params}")

    # בדיקת ניתוק שיחה
    if params.get("hangup") == "yes":
        return ""

    # הנה השורה המעודכנת (שורה 28 בערך):
    audio_path = params.get("record_path") or params.get("path") or params.get("ValName")
    
    token = os.environ.get("YEMOT_TOKEN")

    # --- שלב א: בקשת הקלטה ---
    if not audio_path:
        logger.info("🎤 שולח פקודת record לימות המשיח")
        # שימוש ב-record= כפי שהצעת, כדי לקבל חזרה את הנתיב למשתנה path
        return "record=t-נא להקליט את שאלתכם ובסיום הקישו סולמית&target=path&max=20&beep=yes"

    # --- שלב ב: עיבוד ההקלטה אחרי שהתקבל path ---
    logger.info(f"📂 מזהה הקלטה בנתיב: {audio_path}")
    
    if not token:
        return "id_list_message=t-חסר טוקן של ימות המשיח בשרת"

    client = get_gemini_client()
    if not client:
        return "id_list_message=t-שגיאה בחיבור לבינה המלאכותית"

    tf_path = None
    try:
        # הורדת הקובץ מימות המשיח
        file_url = f"https://www.call2all.co.il/ym/api/DownloadFile?token={token}&path={audio_path}"
        r = requests.get(file_url, timeout=20)
        
        if r.status_code != 200:
            logger.error(f"❌ הורדה נכשלה סטטוס {r.status_code}")
            return "id_list_message=t-לא הצלחתי להוריד את ההקלטה"

        # שמירת הקובץ זמנית לעיבוד
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
            tf.write(r.content)
            tf_path = tf.name

        # שליחה ל-Gemini
        logger.info("🤖 מעבד עם Gemini...")
        with open(tf_path, "rb") as f:
            audio_data = f.read()

        response = client.models.generate_content(
            model="gemini-1.5-flash",
            contents=[
                "ענה בקצרה מאוד, עברית בלבד, בלי תווים מיוחדים",
                {"mime_type": "audio/wav", "data": audio_data}
            ]
        )

        raw_text = response.text if response and response.text else "לא התקבלה תשובה"
        
        # ניקוי הטקסט עבור מנוע הדיבור של ימות המשיח
        clean_text = re.sub(r"[^\u0590-\u05FFa-zA-Z0-9\s\.\,]", "", raw_text).strip()
        logger.info(f"✅ תשובת AI: {clean_text}")

        return f"id_list_message=t-{clean_text}"

    except Exception as e:
        logger.error(f"💥 שגיאה: {str(e)}")
        return "id_list_message=t-אירעה שגיאה בעיבוד השאלה"

    finally:
        if tf_path and os.path.exists(tf_path):
            os.remove(tf_path)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

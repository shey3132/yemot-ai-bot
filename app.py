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
        logger.error("❌ GEMINI_API_KEY missing from environment")
        return None
    return genai.Client(api_key=api_key)

@app.route("/ask_ai", methods=["GET", "POST"])
def ask_ai():
    params = request.values.to_dict()
    logger.info(f"📥 Incoming: {params}")

    # בדיקת ניתוק
    if params.get("hangup") == "yes":
        return ""

    # שליפת נתיב ההקלטה
    audio_path = params.get("path")
    token = os.environ.get("YEMOT_TOKEN")

    # שלב א: אם אין הקלטה - בקש הקלטה
    if not audio_path:
        logger.info("🎤 No audio path - sending record command")
        # משתמשים ב-t כדי שהמערכת תקריא טקסט ולא תחפש קובץ 800
        return "read=t-נא להקליט את שאלתכם ובסיום הקישו סולמית&target=path&max=20&beep=yes"

    # שלב ב: עיבוד ההקלטה
    if not token:
        logger.error("❌ YEMOT_TOKEN missing")
        return "id_list_message=t-חסר מפתח גישה לימות המשיח"

    client = get_gemini_client()
    if not client:
        return "id_list_message=t-חסר מפתח גישה לבינה המלאכותית"

    tf_path = None
    try:
        # הורדת הקובץ
        file_url = f"https://www.call2all.co.il/ym/api/DownloadFile?token={token}&path={audio_path}"
        logger.info(f"📂 Downloading: {audio_path}")
        r = requests.get(file_url, timeout=20)
        
        if r.status_code != 200:
            logger.error(f"❌ Download failed: {r.status_code}")
            return "id_list_message=t-שגיאה בהורדת הקובץ המוקלט"

        # שמירה זמנית
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
            tf.write(r.content)
            tf_path = tf.name

        # שליחה ל-Gemini
        logger.info("🤖 Sending to Gemini...")
        with open(tf_path, "rb") as f:
            audio_data = f.read()

        response = client.models.generate_content(
            model="gemini-1.5-flash",
            contents=[
                "ענה בקצרה מאוד ובעברית בלבד",
                {"mime_type": "audio/wav", "data": audio_data}
            ]
        )

        ans = response.text if response and response.text else "לא הצלחתי להבין את השאלה"
        
        # ניקוי תווים מיוחדים שיכולים לשבש את ימות המשיח
        clean_ans = re.sub(r"[^\u0590-\u05FFa-zA-Z0-9\s\.\,\?]", "", ans).strip()
        logger.info(f"✅ AI Response: {clean_ans}")

        return f"id_list_message=t-{clean_ans}"

    except Exception as e:
        logger.error(f"💥 Critical Error: {str(e)}")
        logger.error(traceback.format_exc())
        return "id_list_message=t-אירעה שגיאה בעיבוד הנתונים"

    finally:
        if tf_path and os.path.exists(tf_path):
            os.remove(tf_path)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

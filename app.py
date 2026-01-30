from flask import Flask, request
import os
import requests
import tempfile
import logging
import re
import traceback
from google import genai

# ================== App ==================
app = Flask(__name__)

# ================== Logging ==================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("yemot-ai")

# ================== Gemini ==================
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")
if not GEMINI_KEY:
    logger.error("❌ GEMINI_API_KEY לא מוגדר")

client = genai.Client(api_key=GEMINI_KEY)

# ================== Utils ==================
HEBREW_CLEAN_RE = re.compile(r"[^\u0590-\u05FFa-zA-Z0-9\s\.\,\?\!]")
MAX_TEXT_LEN = 250


def clean_text(text: str) -> str:
    text = HEBREW_CLEAN_RE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:MAX_TEXT_LEN]


# ================== Route ==================
@app.route("/ask_ai", methods=["GET", "POST"])
def ask_ai():
    params = dict(request.values)
    logger.info("PARAMS: %s", params)

    # --- ניתוק שיחה ---
    if params.get("hangup") == "yes":
        logger.info("☎️ ניתוק שיחה – לא מבצע פעולה")
        return ""

    audio_path = params.get("path")
    token = os.environ.get("YEMOT_TOKEN")

    # --- אין path: לא מבקשים הקלטה כאן ---
    # אם השלוחה מוגדרת כ-record – ימות כבר מקליט לבד
    if not audio_path:
        logger.warning("⚠️ אין path – ממתין להקלטה")
        return ""

    if not token:
        logger.error("❌ YEMOT_TOKEN לא מוגדר")
        return "id_list_message=t-תקלה טכנית"

    if not audio_path.startswith("ivr2/"):
        logger.error("❌ path לא חוקי: %s", audio_path)
        return "id_list_message=t-תקלה בהקלטה"

    tf_path = None

    try:
        # ================== הורדת הקלטה ==================
        file_url = (
            "https://www.call2all.co.il/ym/api/DownloadFile"
            f"?token={token}&path={audio_path}"
        )

        r = requests.get(file_url, timeout=15)
        if r.status_code != 200 or not r.content:
            logger.error("❌ הורדת הקלטה נכשלה (%s)", r.status_code)
            return "id_list_message=t-לא הצלחתי לשמוע"

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
            tf.write(r.content)
            tf_path = tf.name

        logger.info("✅ הקלטה נשמרה: %s", tf_path)

        # ================== Gemini ==================
        uploaded = client.files.upload(tf_path)

        response = client.models.generate_content(
            model="gemini-1.5-flash",
            contents=[
                "ענה בקצרה ובעברית פשוטה בלבד",
                uploaded
            ],
            generation_config={
                "temperature": 0.2,
                "max_output_tokens": 120
            }
        )

        text = response.text if response and response.text else ""
        text = clean_text(text)

        if not text:
            logger.warning("⚠️ Gemini החזיר טקסט ריק")
            return "id_list_message=t-לא הצלחתי להבין"

        logger.info("🤖 תשובת AI: %s", text)

        # ================== החזרה ל-Yemot ==================
        return f"id_list_message=t-{text}"

    except Exception:
        logger.error("❌ שגיאה כללית:\n%s", traceback.format_exc())
        return "id_list_message=t-אירעה תקלה"

    finally:
        if tf_path and os.path.exists(tf_path):
            os.remove(tf_path)
            logger.info("🧹 קובץ זמני נמחק")


# ================== Run ==================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

from flask import Flask, request
import os
import requests
import tempfile
import logging
import re
import traceback

from google import genai

app = Flask(__name__)

# ---------- לוגינג ----------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------- Gemini ----------
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")
if not GEMINI_KEY:
    logger.error("GEMINI_API_KEY לא מוגדר")
client = genai.Client(api_key=GEMINI_KEY)

# ---------- Route ----------
@app.route("/ask_ai", methods=["GET", "POST"])
def ask_ai():
    # לוג מלא – קריטי
    logger.info("PARAMS: %s", dict(request.values))

    audio_path = request.values.get("path")
    token = os.environ.get("YEMOT_TOKEN")

    # שלב 1 – בקשת הקלטה
    if not audio_path:
        return "read=f-800&target=path&max=20&beep=yes"

    if not token:
        return "id_list_message=t-חלה תקלה טכנית"

    tf_path = None

    try:
        # שלב 2 – הורדת ההקלטה
        file_url = (
            "https://www.call2all.co.il/ym/api/DownloadFile"
            f"?token={token}&path={audio_path}"
        )

        r = requests.get(file_url, timeout=15)
        if r.status_code != 200:
            logger.error("הורדה נכשלה: %s", r.status_code)
            return "id_list_message=t-חלה תקלה טכנית"

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
            tf.write(r.content)
            tf_path = tf.name

        # שלב 3 – Gemini
        uploaded = client.files.upload(tf_path)

        response = client.models.generate_content(
            model="gemini-1.5-flash",
            contents=[
                "ענה בקצרה בעברית",
                uploaded
            ]
        )

        text = response.text if response and response.text else ""
        text = re.sub(r"[^\w\u0590-\u05FF\s\.\,\-]", "", text).strip()

        if not text:
            return "id_list_message=t-לא הצלחתי להבין"

        # שלב 4 – החזרה לימות המשיח
        return f"id_list_message=t-{text}"

    except Exception as e:
        logger.error("שגיאה:\n%s", traceback.format_exc())
        return "id_list_message=t-חלה תקלה טכנית"

    finally:
        if tf_path and os.path.exists(tf_path):
            os.remove(tf_path)


# ---------- Run ----------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

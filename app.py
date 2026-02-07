# -*- coding: utf-8 -*-
from flask import Flask, request
import os, requests, tempfile, logging, re
from google import genai

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("yemot-ai")

app = Flask(__name__)

PROMPT_TEXT = (
    "תענה בקצרה בעברית ופתח את השיחה בברכת שלום קצרה."
)

GREETING_TEXT = "שלום, איך אני יכול לעזור לך היום?"

@app.route("/ask_ai", methods=["GET", "POST"])
def ask_ai():
    params = request.values.to_dict()
    logger.info(f"📥 Params: {params}")

    if params.get("hangup") == "yes":
        return ""

    audio_path = params.get("path")
    token = os.environ.get("YEMOT_TOKEN")
    api_key = os.environ.get("GEMINI_API_KEY")

    if not token:
        return "id_list_message=t-חסר YEMOT_TOKEN בשרת"

    if not api_key:
        return "id_list_message=t-חסר GEMINI_API_KEY בשרת"

    # אין הקלטה → בקש מהמשתמש לדבר
    if not audio_path:
        return "read=f-800&target=path&max=20&beep=yes&type=recording"

    try:
        client = genai.Client(api_key=api_key)

        file_url = f"https://www.call2all.co.il/ym/api/DownloadFile?token={token}&path={audio_path}"
        r = requests.get(file_url, timeout=30)
        r.raise_for_status()

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
            tf.write(r.content)
            tf_path = tf.name

        with open(tf_path, "rb") as f:
            audio_data = f.read()

        contents = [
            GREETING_TEXT,          # ג׳ימיני מתחיל את השיחה
            PROMPT_TEXT,            # הנחיה כללית
            {"mime_type": "audio/wav", "data": audio_data},
        ]

        response = client.models.generate_content(
            model="gemini-1.5-flash",
            contents=contents,
        )

        ans = re.sub(r"[^\u0590-\u05FFa-zA-Z0-9\s]", "", response.text).strip()

        if not ans.startswith("שלום"):
            ans = f"שלום {ans}"

        return f"id_list_message=t-{ans}"

    except Exception as e:
        logger.error(f"Error: {e}")
        return "id_list_message=t-תקלה בעיבוד"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))

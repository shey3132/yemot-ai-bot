from flask import Flask, request, jsonify
import os, requests, tempfile, logging, re, traceback
from google import genai

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("yemot-ai")

GEMINI_KEY = os.environ.get("GEMINI_API_KEY")
YEMOT_TOKEN = os.environ.get("YEMOT_TOKEN")

client = genai.Client(api_key=GEMINI_KEY) if GEMINI_KEY else None

@app.route("/ask_ai", methods=["GET", "POST"])
def ask_ai():
    # ימות המשיח לפעמים שולחים ב-GET ולפעמים ב-POST
    params = request.values.to_dict()
    logger.info(f"Received Request: {params}")

    # 1. בדיקת ניתוק
    if params.get("hangup") == "yes":
        return ""

    # 2. שליפת הנתיב (בדיקה של כמה שמות משתנים אפשריים)
    audio_path = params.get("path") or params.get("file_path") or params.get("record_path")
    
    if not audio_path:
        logger.warning("No audio path found in request")
        return "id_list_message=t-נא להקליט שאלה בסיום הסולמית"

    if not YEMOT_TOKEN:
        return "id_list_message=t-חסר טוקן שרת"

    tf_path = None
    try:
        # 3. הורדת הקובץ
        file_url = f"https://www.call2all.co.il/ym/api/DownloadFile?token={YEMOT_TOKEN}&path={audio_path}"
        r = requests.get(file_url, timeout=20)
        
        if r.status_code != 200:
            return f"id_list_message=t-שגיאת הורדה {r.status_code}"

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
            tf.write(r.content)
            tf_path = tf.name

        # 4. Gemini
        uploaded = client.files.upload(path=tf_path)
        response = client.models.generate_content(
            model="gemini-1.5-flash",
            contents=["ענה בקצרה מאוד בעברית", uploaded]
        )

        text = response.text if response and response.text else "לא הצלחתי להבין"
        clean = re.sub(r"[^\u0590-\u05FFa-zA-Z0-9\s\.\,]", "", text).strip()

        return f"id_list_message=t-{clean}"

    except Exception as e:
        logger.error(traceback.format_exc())
        return "id_list_message=t-אירעה שגיאה בעיבוד"
    finally:
        if tf_path and os.path.exists(tf_path):
            os.remove(tf_path)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))

from flask import Flask, request, make_response
import os, requests, tempfile
import google.generativeai as genai

app = Flask(__name__)
genai.configure(api_key=os.environ.get("GEMINI_API_KEY"))

@app.route('/ask_ai', methods=['GET', 'POST'])
def ask_ai():
    audio_path = request.values.get('path')

    # פקודת הקלטה פשוטה ללא תווים מיוחדים
    if not audio_path:
        return "read=t-נא לדבר לאחר הצפצוף ובסיום סולמית&target=path&max=20&beep=yes"

    try:
        # הורדה
        token = os.environ.get("YEMOT_TOKEN")
        url = f"https://www.call2all.co.il/ym/api/DownloadFile?token={token}&path={audio_path}"
        res = requests.get(url)
        
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp.write(res.content)
            tmp_path = tmp.name

        # בינה מלאכותית
        model = genai.GenerativeModel("gemini-1.5-flash")
        audio_file = genai.upload_file(path=tmp_path)
        answer = model.generate_content(["תענה בקצרה בעברית", audio_file])
        
        # ניקוי מוחלט של התשובה
        clean_text = "".join(c for c in answer.text if c.isalnum() or c in " .")
        
        # פקודת השמעה
        return f"id_list_message=t-{clean_text}"

    except Exception as e:
        return "id_list_message=t-חלה שגיאה בעיבוד"

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)

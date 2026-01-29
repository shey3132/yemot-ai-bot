import requests
import google.generativeai as genai
from flask import Flask, request
import os

# משיכת המפתח מהגדרות השרת ב-Render
api_key = os.environ.get('GOOGLE_API_KEY')
genai.configure(api_key=api_key)

app = Flask(__name__)

@app.route('/ask_ai', methods=['GET', 'POST'])
def handle_call():
    file_url = request.values.get('path')
    if not file_url:
        return "id_list_message=f-שלום, לא הועברה הקלטה מהמערכת."

    try:
        # הורדת קובץ השמע זמנית לשרת
        audio_response = requests.get(file_url)
        with open("input.wav", "wb") as f:
            f.write(audio_response.content)

        # שליחה ל-Gemini
        model = genai.GenerativeModel("gemini-1.5-flash")
        audio_file = genai.upload_file(path="input.wav")
        
        # הנחיה לבינה המלאכותית
        response = model.generate_content([audio_file, "ענה בקצרה מאוד ובעברית על השאלה שנשאלה בשמע."])
        
        return f"id_list_message=f-{response.text}"
    except Exception as e:
        return "id_list_message=f-חלה שגיאה בעיבוד השאלה, נסו שוב."

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))

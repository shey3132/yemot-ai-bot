from flask import Flask, request
import google.generativeai as genai
import os

app = Flask(__name__)
genai.configure(api_key=os.environ.get("GEMINI_API_KEY"))

@app.route('/ask_ai', methods=['GET', 'POST'])
def ask_ai():
    # קבלת הטקסט שזוהה על ידי ימות המשיח
    user_text = request.values.get('text_question')

    # אם ימות המשיח לא הצליחו לזהות דיבור (שקט)
    if not user_text or user_text.strip() == "":
        return "id_list_message=t-לא שמעתי שאלה, נא לנסות שנית"

    try:
        model = genai.GenerativeModel("gemini-1.5-flash")
        # שליחת הטקסט לבינה המלאכותית
        response = model.generate_content(f"תענה בקצרה מאוד בעברית: {user_text}")

        # ניקוי תווים שמשבשים את מנוע הדיבור (חשוב מאוד!)
        # הסרנו כוכביות, סולמיות וסימני < >
        clean_ans = "".join(c for c in response.text if c.isalnum() or c in " .")

        return f"id_list_message=t-{clean_ans}"

    except Exception as e:
        print(f"Error: {e}") # הדפסה ללוג של Render כדי שתוכל לראות אם יש שגיאה
        return "id_list_message=t-חלה שגיאה בחיבור לבינה המלאכותית"

if __name__ == '__main__':
    # Render מחייב שימוש בפורט מהסביבה או 10000
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)

from flask import Flask, request
import os

app = Flask(__name__)

@app.route('/ask_ai', methods=['GET', 'POST'])
def ask_ai():
    # שליפת הפרמטרים מימות המשיח (מופיעים בלוג כ-ApiSend)
    audio_path = request.values.get('path')
    
    # שלב א: אם אין עדיין הקלטה, נשלח פקודה לימות המשיח להקליט
    if not audio_path:
        # הפקודה הזו אומרת לימות המשיח: תשמיעו הודעה, תצפצפו ותקליטו לתוך משתנה שנקרא path
        return "read=t-נא להגיד את שאלתכם לאחר הצפצוף ובסיום להקיש סולמית&target=path&max=30&beep=yes"

    # שלב ב: אם הגענו לכאן, סימן שיש הקלטה בתוך audio_path
    try:
        # כאן יבוא הקוד של ה-AI (התמלול והתשובה של ג'מיני)
        # כרגע נחזיר תשובה זמנית לבדיקה כדי לראות שההקלטה עברה
        return f"id_list_message=t-ההקלטה התקבלה בהצלחה. הנתיב שלה הוא {audio_path}"
        
    except Exception as e:
        return "id_list_message=t-חלה שגיאה בעיבוד התמלול"

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))

import os
import time
import tempfile
import requests
import re
import threading
import json
import sqlite3

from flask import Flask, request, Response
from groq import Groq

app = Flask(__name__)

# הגדרות סביבה
YEMOT_TOKEN = os.environ.get("YEMOT_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GOOGLE_SCRIPT_URL = os.environ.get("GOOGLE_SCRIPT_URL")
TARGET_EMAIL = os.environ.get("TARGET_EMAIL")

client = Groq(api_key=GROQ_API_KEY)

# פקודת הקלטה לימות המשיח
RECORD_COMMAND = "user_audio,no,record,,,yes,yes,no,1,120"
DB_FILE = "chat_memory.db"


def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS conversations (
            caller_id TEXT PRIMARY KEY,
            name TEXT,
            history TEXT
        )
    ''')
    conn.commit()
    conn.close()

# אתחול מסד הנתונים
init_db()


def get_chat_data(caller_id):
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT history, name FROM conversations WHERE caller_id = ?", (caller_id,))
        row = cursor.fetchone()
        conn.close()
        if row and row[0]:
            return json.loads(row[0]), row[1]
    except Exception as e:
        print("DB GET ERROR:", e)
    return [], None


def save_chat_data(caller_id, history, name):
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        history_json = json.dumps(history)
        cursor.execute('''
            INSERT INTO conversations (caller_id, history, name)
            VALUES (?, ?, ?)
            ON CONFLICT(caller_id)
            DO UPDATE SET history=excluded.history,
            name=COALESCE(excluded.name, conversations.name)
        ''', (caller_id, history_json, name))
        conn.commit()
        conn.close()
    except Exception as e:
        print("DB SAVE ERROR:", e)


def delete_chat_data(caller_id):
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM conversations WHERE caller_id = ?", (caller_id,))
        conn.commit()
        conn.close()
    except Exception as e:
        print("DB DELETE ERROR:", e)


def clean_text(text):
    # חוזרים לניקוי מוחלט! פסיקים וסימנים מיוחדים יגרמו לניתוק השיחה בימות המשיח
    text = re.sub(r'[^\u0590-\u05FFa-zA-Z0-9\s]', '', text)
    return " ".join(text.split())


def quick_answer(user_text):
    t = user_text.lower()
    if "מה השעה" in t:
        return f"השעה עכשיו {time.strftime('%H:%M')} תרצה לשאול משהו נוסף"
    if "מה התאריך" in t:
        return f"התאריך היום {time.strftime('%d/%m/%Y')} במה עוד אוכל לעזור"
    return None


def send_summary_email(caller_id, history, name):
    if not GOOGLE_SCRIPT_URL or not TARGET_EMAIL:
        return
    try:
        display_name = name if name else "משתמש לא ידוע"
        subject = f"📄 סיכום שיחה מלא - נועם AI: {display_name} ({caller_id})"
        
        body = """
        <div style="font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; direction: rtl; max-width: 650px; margin: 20px auto; border: 1px solid #eaeaea; border-radius: 8px; box-shadow: 0 4px 6px rgba(0,0,0,0.05); overflow: hidden; background-color: #ffffff;">
            <div style="background-color: #0f172a; color: #ffffff; padding: 25px; text-align: center; border-bottom: 4px solid #3b82f6;">
                <h2 style="margin: 0; font-size: 24px; font-weight: 600;">סיכום שיחה נכנסת - נועם AI</h2>
            </div>
            <div style="padding: 20px; background-color: #f8fafc; border-bottom: 1px solid #e2e8f0; font-size: 15px; color: #334155;">
                <table style="width: 100%; direction: rtl;">
                    <tr>
                        <td style="padding: 5px 0;"><b>שם מתקשר:</b> """ + display_name + """</td>
                        <td style="padding: 5px 0;"><b>מספר טלפון:</b> <span style="direction: ltr; display: inline-block;">""" + caller_id + """</span></td>
                    </tr>
                    <tr>
                        <td style="padding: 5px 0;" colspan="2"><b>תאריך ושעה:</b> <span style="direction: ltr; display: inline-block;">""" + time.strftime('%d/%m/%Y %H:%M') + """</span></td>
                    </tr>
                </table>
            </div>
            <div style="padding: 25px; color: #1e293b; font-size: 15px; line-height: 1.6;">
                <h3 style="margin-top: 0; border-bottom: 2px solid #cbd5e1; padding-bottom: 10px; color: #0f172a;">היסטוריית ההודעות המלאה:</h3>
        """
        for msg in history:
            if msg["role"] == "user":
                body += f'<div style="margin-bottom: 15px; padding: 12px 15px; background-color: #e0f2fe; border-right: 4px solid #0ea5e9; border-radius: 4px;"><strong style="color: #0284c7;">משתמש:</strong><br>{msg["content"]}</div>'
            else:
                body += f'<div style="margin-bottom: 15px; padding: 12px 15px; background-color: #f1f5f9; border-right: 4px solid #64748b; border-radius: 4px;"><strong style="color: #475569;">נועם:</strong><br>{msg["content"]}</div>'
        
        body += """
            </div>
            <div style="text-align: center; padding: 15px; background-color: #f8fafc; font-size: 13px; color: #64748b; border-top: 1px solid #e2e8f0;">
                הודעה זו הופקה ונשלחה אוטומטית על ידי מערכת נועם AI.
            </div>
        </div>
        """
        requests.post(GOOGLE_SCRIPT_URL, json={"to": TARGET_EMAIL, "subject": subject, "htmlBody": body}, timeout=12)
    except Exception as e:
        print("EMAIL ERROR:", e)


@app.route('/ai-chat', methods=['GET', 'POST'])
def ai_chat():
    caller_id = request.values.get('ApiPhone', 'unknown')
    history, known_name = get_chat_data(caller_id)

    if request.values.get('hangup') == 'yes':
        if history:
            threading.Thread(target=send_summary_email, args=(caller_id, history, known_name)).start()
        delete_chat_data(caller_id)
        return Response("noop", mimetype='text/plain')

    audio_list = request.values.getlist('user_audio')
    audio_path = audio_list[-1] if audio_list else None

    if not audio_path:
        return Response(f"read=t-שלום כאן נועם במה אוכל לעזור לך היום דברו לאחר הצליל={RECORD_COMMAND}", mimetype='text/plain')

    yemot_path = f"ivr2:{audio_path}"
    tmp_file = None

    try:
        audio = requests.get("https://www.call2all.co.il/ym/api/DownloadFile", params={"token": YEMOT_TOKEN, "path": yemot_path}, timeout=20)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(audio.content)
            tmp_file = f.name

        with open(tmp_file, "rb") as f:
            transcript = client.audio.transcriptions.create(
                file=f,
                model="whisper-large-v3",
                language="he",
                prompt="היי, זו שיחה טלפונית בעברית עם עוזר קולי בשם נועם. מילים נפוצות: נועם, עוזר קולי, ימות המשיח, תמלול.",
                temperature=0.0
            )

        user_text = transcript.text.strip()

        quick = quick_answer(user_text)
        if quick:
            return Response(f"read=t-{clean_text(quick)}={RECORD_COMMAND}", mimetype='text/plain')

        history.append({"role": "user", "content": user_text})

        # ה-System Prompt שמכריח אותו לסיים בשאלה (הטקסט ינוקה מסימני שאלה בקוד, אך המילים יישארו כשאלה)
        system_prompt = (
            "אתה נועם, עוזר קולי חכם ויעיל בטלפון. "
            "התשובות שלך מוקראות למשתמש דרך מערכת טקסט-לדיבור (TTS), ולכן עליך לעמוד בכללים הבאים באופן מוחלט:\n"
            "1. ענה תמיד בקצר, לעניין ובמשפטים פשוטים - מקסימום 2-3 משפטים לתשובה. אל תאריך במילים.\n"
            "2. אל תשתמש בשום עיצוב טקסט ובשום סימן פיסוק (בלי כוכביות, בלי פסיקים, נקודות או סימני שאלה).\n"
            "3. אל תציג רשימות ממוספרות (1, 2, 3) או נקודות. תאר את האפשרויות במשפט זורם אחד.\n"
            "4. הטקסט שאתה מקבל מגיע מתמלול טלפוני ועשוי להכיל שגיאות כתיב קשות - התעלם מהשגיאות וחלץ את כוונת המשתמש מההקשר.\n"
            "5. שמור על טון אדיב וידידותי, ללא גינוני נימוס ארוכים מדי שמבזבזים זמן.\n"
            "6. כלל ברזל: עליך לסיים תמיד, ללא יוצא מן הכלל, את התשובה שלך בשאלה קצרה שמניעה לפעולה (למשל: 'איך עוד אוכל לעזור לך', 'תרצה לשמוע פרטים על כך', 'מה תרצה שנעשה עכשיו')."
        )

        chat = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "system", "content": system_prompt}] + history[-10:],
            temperature=0.4,
            max_tokens=150
        )

        ai_reply = chat.choices[0].message.content.strip()
        history.append({"role": "assistant", "content": ai_reply})
        save_chat_data(caller_id, history, known_name)

        return Response(f"read=t-{clean_text(ai_reply)}={RECORD_COMMAND}", mimetype='text/plain')

    except Exception as e:
        print("GLOBAL ERROR:", e)
        return Response(f"read=t-סליחה תקלה זמנית בעיבוד הנתונים אנא נסו שוב בשנית={RECORD_COMMAND}", mimetype='text/plain')

    finally:
        if tmp_file and os.path.exists(tmp_file):
            try:
                os.remove(tmp_file)
            except Exception as e:
                print("Temporary file removal error:", e)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)

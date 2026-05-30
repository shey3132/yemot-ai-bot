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
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY") # מפתח לחיפוש גוגל (Custom Search API)
GOOGLE_CX = os.environ.get("GOOGLE_CX") # מזהה מנוע חיפוש של גוגל

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
    # הסרת כל תווי הבקרה שגורמים לתקיעות בימות המשיח: נקודה, מקף, שווה, אמפרסנד וכו'
    text = re.sub(r'[\.\-\=&,]', '', text)
    # ניקוי כללי משאר תווים מיוחדים
    text = re.sub(r'[^\u0590-\u05FFa-zA-Z0-9\s]', '', text)
    return " ".join(text.split())


def quick_answer(user_text):
    t = user_text.lower()
    if "מה השעה" in t:
        return f"השעה עכשיו {time.strftime('%H:%M')} תרצה לשאול משהו נוסף"
    if "מה התאריך" in t:
        return f"התאריך היום {time.strftime('%d/%m/%Y')} במה עוד אוכל לעזור"
    return None


def perform_google_search(query):
    if not GOOGLE_API_KEY or not GOOGLE_CX:
        return "מנגנון החיפוש לא הוגדר במערכת"
    
    url = f"https://www.googleapis.com/customsearch/v1?q={query}&key={GOOGLE_API_KEY}&cx={GOOGLE_CX}"
    try:
        res = requests.get(url, timeout=10).json()
        items = res.get("items", [])
        if not items:
            return "לא נמצאו תוצאות לחיפוש זה"
        
        results = [f"{item['title']} - {item['snippet']}" for item in items[:2]]
        return "תוצאות מהרשת: " + " ".join(results)
    except Exception as e:
        print("GOOGLE SEARCH ERROR:", e)
        return "הייתה שגיאה בחיפוש ברשת"


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
            role_display = "משתמש" if msg["role"] == "user" else "מערכת" if msg["role"] == "tool" else "נועם"
            color_bg = "#e0f2fe" if msg["role"] == "user" else "#fef3c7" if msg["role"] == "tool" else "#f1f5f9"
            color_border = "#0ea5e9" if msg["role"] == "user" else "#f59e0b" if msg["role"] == "tool" else "#64748b"
            color_text = "#0284c7" if msg["role"] == "user" else "#d97706" if msg["role"] == "tool" else "#475569"
            
            # אל תציג קריאות לפונקציות (רק את התוכן של ההודעות)
            if "tool_calls" not in msg:
                content = msg.get("content", "")
                body += f'<div style="margin-bottom: 15px; padding: 12px 15px; background-color: {color_bg}; border-right: 4px solid {color_border}; border-radius: 4px;"><strong style="color: {color_text};">{role_display}:</strong><br>{content}</div>'
        
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

        # הגדרת אופי, זרימה דינמית ומניעת סימני פיסוק
        system_prompt = (
            "אתה נועם עוזר קולי של הארגון מדבר בטלפון עם המשתמש "
            "אתה מדבר בגובה העיניים בשפה פשוטה זורמת ויומיומית "
            "אל תאשר קבלה של כל משפט אל תגיד אוקיי או הבנתי פשוט תענה ישר ולעניין "
            "איסור מוחלט על סימני פיסוק אל תשתמש בנקודה פסיק מקף שווה אמפרסנד או סימן שאלה בתשובה שלך "
            "אם יש צורך במספרים קרא אותם ללא סמלים "
            "תמיד תסיים בשאלה קצרה שמניעה להמשך שיחה "
            "דוגמה לשיחה נכונה "
            "משתמש נועם איפה התרומה שלי "
            "נועם אני רואה שהיא נקלטה במערכת תרצה שאשלח קבלה "
            "משתמש כן תודה "
            "נועם מעולה שלחתי יש עוד משהו שתרצה עזרה איתו "
        )

        tools = [
            {
                "type": "function",
                "function": {
                    "name": "google_search",
                    "description": "ביצוע חיפוש באינטרנט במנוע של גוגל כדי למצוא עובדות, מידע עדכני או נתונים בזמן אמת.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "מילת החיפוש המדויקת לגוגל"
                            }
                        },
                        "required": ["query"]
                    }
                }
            }
        ]

        # קריאה ראשונה לגרוק
        chat = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "system", "content": system_prompt}] + history[-10:],
            temperature=0.4,
            max_tokens=150,
            tools=tools,
            tool_choice="auto"
        )

        response_message = chat.choices[0].message
        
        # בדיקה אם המודל החליט שצריך חיפוש בגוגל
        if response_message.tool_calls:
            # הוספת קריאת הכלים להיסטוריה כדי שהמודל יבין את ההקשר
            tool_calls_dict = []
            for t in response_message.tool_calls:
                tool_calls_dict.append({
                    "id": t.id,
                    "type": "function",
                    "function": {
                        "name": t.function.name,
                        "arguments": t.function.arguments
                    }
                })
                
            history.append({
                "role": "assistant",
                "content": response_message.content,
                "tool_calls": tool_calls_dict
            })

            for tool_call in response_message.tool_calls:
                if tool_call.function.name == "google_search":
                    args = json.loads(tool_call.function.arguments)
                    search_result = perform_google_search(args["query"])
                    
                    history.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": "google_search",
                        "content": search_result
                    })
            
            # קריאה שנייה לגרוק אחרי קבלת התוצאות מהחיפוש
            chat = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "system", "content": system_prompt}] + history[-12:],
                temperature=0.4,
                max_tokens=150
            )
            ai_reply = chat.choices[0].message.content.strip()
            history.append({"role": "assistant", "content": ai_reply})
        
        else:
            # אם לא היה חיפוש, פשוט שומרים את התשובה
            ai_reply = response_message.content.strip()
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

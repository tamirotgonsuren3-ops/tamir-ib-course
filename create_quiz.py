import os
import sys
import json
import sqlite3
import argparse
from typing import List, Optional
from html.parser import HTMLParser
from pydantic import BaseModel, Field

# Google GenAI Official SDK
from google import genai
from google.genai import types

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "edupath.db")
UPLOAD_LESSONS = os.path.join(BASE_DIR, "uploads", "lessons")
os.makedirs(UPLOAD_LESSONS, exist_ok=True)

# --- PYDANTIC SCHEMAS (Web App-тай 100% нийцтэй) ---
class QuizQuestion(BaseModel):
    id: int
    question_type: str = Field(description="'multiple_choice' or 'written_response'")
    target_criteria: str = Field(description="'Criterion A', 'Criterion B', 'Criterion C', or 'Criterion D'")
    question_text: str
    options: Optional[List[str]] = Field(default=None, description="4 choices for multiple choice")
    correct_option_index: Optional[int] = Field(default=None, description="Index 0-3 if multiple choice")
    rubric_guidance: str = Field(description="IB evaluation benchmark criteria")

class GeneratedQuiz(BaseModel):
    quiz_title: str
    questions: List[QuizQuestion]

# --- HTML ТЕКСТ ЯЛГАГЧ (Нэмэлт сан шаардахгүй) ---
class HTMLTextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text_parts = []
        self.in_script_or_style = False

    def handle_starttag(self, tag, attrs):
        if tag in ["script", "style"]:
            self.in_script_or_style = True
        elif tag in ["h1", "h2", "h3", "h4", "tr", "div", "p", "li"]:
            self.text_parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ["script", "style"]:
            self.in_script_or_style = False

    def handle_data(self, data):
        if not self.in_script_or_style:
            cleaned = data.strip()
            if cleaned:
                self.text_parts.append(cleaned + " ")

    def get_text(self):
        return "".join(self.text_parts).strip()

def extract_content_from_html(html_path: str) -> str:
    with open(html_path, "r", encoding="utf-8") as f:
        html_content = f.read()
    parser = HTMLTextExtractor()
    parser.feed(html_content)
    return parser.get_text()

# --- DATABASE & API KEY HELPERS ---
def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def resolve_api_key(cli_key: Optional[str]) -> str:
    if cli_key and cli_key.strip():
        return cli_key.strip()
    
    # 1. Check environment variable
    env_key = os.environ.get("GEMINI_API_KEY")
    if env_key:
        return env_key.strip()

    # 2. Check Database for registered teacher key
    if os.path.exists(DB_PATH):
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT gemini_api_key FROM users WHERE role = 'teacher' AND gemini_api_key IS NOT NULL AND gemini_api_key != '' LIMIT 1")
        row = c.fetchone()
        conn.close()
        if row and row["gemini_api_key"]:
            return row["gemini_api_key"]

    print("❌ Алдаа: Gemini API Key олдсонгүй!")
    print("Шийдэл: --api_key аргументаар өгөх эсвэл Вэб системдээ багшаар нэвтрэн API түлхүүрээ хадгална уу.")
    sys.exit(1)

def ensure_lesson_in_db(html_path: str, lesson_title: str, teacher_id: int) -> int:
    conn = get_db_connection()
    c = conn.cursor()
    filename = os.path.basename(html_path)

    # Хичээл өмнө нь бүртгэгдсэн эсэхийг шалгах
    c.execute("SELECT id FROM lessons WHERE filename = ?", (filename,))
    row = c.fetchone()
    if row:
        lesson_id = row["id"]
    else:
        # Хуулж хадгалах
        dest_path = os.path.join(UPLOAD_LESSONS, filename)
        if not os.path.exists(dest_path):
            import shutil
            shutil.copy2(html_path, dest_path)

        c.execute(
            "INSERT INTO lessons (title, filename, teacher_id) VALUES (?, ?, ?)",
            (lesson_title, filename, teacher_id)
        )
        conn.commit()
        lesson_id = c.lastrowid
        print(f"📖 Шинэ хичээл амжилттай бүртгэгдлээ: [ID: {lesson_id}] {lesson_title}")
    
    conn.close()
    return lesson_id

# --- MAIN GENERATION FUNCTION ---
def generate_and_save_quiz(
    html_file_input: str,
    quiz_title: Optional[str] = None,
    api_key: Optional[str] = None,
    teacher_id: int = 1,
    num_mcq: int = 3,
    num_written: int = 2
):
    if not os.path.exists(html_file_input):
        print(f"❌ Алдаа: '{html_file_input}' файл олдсонгүй.")
        sys.exit(1)

    print(f"📄 HTML файлыг уншиж байна: {html_file_input}...")
    extracted_text = extract_content_from_html(html_file_input)

    # Гарчиг автоматаар үүсгэх
    if not quiz_title:
        base_name = os.path.splitext(os.path.basename(html_file_input))[0]
        quiz_title = f"{base_name.capitalize()} - IB Үнэлгээний Сорил"

    resolved_key = resolve_api_key(api_key)
    print("🔑 Gemini API түлхүүр баталгаажлаа.")

    # 1. Хичээлийг DB-д холбох
    lesson_id = ensure_lesson_in_db(html_file_input, quiz_title, teacher_id)

    # 2. Gemini Prompt бэлтгэх
    prompt = f"""
    Та бол олон улсын бакалавр (IB - International Baccalaureate) болон Бизнес, Нягтлан бодох бүртгэлийн мэргэжлийн багш.
    Дараах сургалтын танилцуулга слайдын агуулгыг үндэслэн 10-р ангийн сурагчдад зориулсан шалгалт үүсгэнэ үү.

    ШАЛГАЛТЫН ШААРДЛАГА:
    1. Нийт {num_mcq} сонгох асуулт (multiple_choice), тус бүр 4 сонголттой ба зөв хариултын индексийг (0-3) заана.
    2. Нийт {num_written} задгай бичих асуулт (written_response) үүсгэнэ.
    3. Асуултууд нь дараах сэдвүүдийг хамрах ёстой:
       - Нягтлан бодохын алтан дүрэм (Актив = Өр төлбөр + Эздийн өмч), Дебит ба Кредит бичилт
       - Татварууд: НӨАТ (10% Оролт/Гаралт), ААННОАТ (Ашгийн татвар 10%)
       - Цалин бодолт: Нийгмийн даатгалын шимтгэл (НДШ 11.5% ажилтан, 12.5% ажил олгогч, ЭМД 2%), ХХОАТ
       - Талхны үйлдвэр (3-р баг) эсвэл бусад бизнесийн бодит тооцоолол, кейс
    4. Асуулт бүрийг IB MYP/DP үнэлгээний шалгуурт (Criterion A, Criterion B, Criterion C, эсвэл Criterion D) заавал оноож, rubric_guidance талбарт сурагчийн хариултаас юу шаардахыг бичнэ үү.

    СЛАЙДЫН БҮХ АГУУЛГА:
    \"\"\"
    {extracted_text}
    \"\"\"
    """

    print("🤖 Gemini 2.5 Flash руу хүсэлт илгээж байна...")
    client = genai.Client(api_key=resolved_key)

    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=GeneratedQuiz,
                temperature=0.2
            ),
        )
        parsed_quiz = json.loads(re
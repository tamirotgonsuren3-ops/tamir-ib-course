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

# --- HTML ТЕКСТ ЯЛГАГЧ ---
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
    
    env_key = os.environ.get("GEMINI_API_KEY")
    if env_key:
        return env_key.strip()

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

def ensure_lesson_in_db(file_path: str, lesson_title: str, teacher_id: int) -> int:
    conn = get_db_connection()
    c = conn.cursor()
    filename = os.path.basename(file_path)

    c.execute("SELECT id FROM lessons WHERE filename = ?", (filename,))
    row = c.fetchone()
    if row:
        lesson_id = row["id"]
    else:
        dest_path = os.path.join(UPLOAD_LESSONS, filename)
        if not os.path.exists(dest_path):
            import shutil
            shutil.copy2(file_path, dest_path)

        c.execute(
            "INSERT INTO lessons (title, filename, teacher_id) VALUES (?, ?, ?)",
            (lesson_title, filename, teacher_id)
        )
        conn.commit()
        lesson_id = c.lastrowid
        print(f"📖 Шинэ хичээл/материал бүртгэгдлээ: [ID: {lesson_id}] {lesson_title}")
    
    conn.close()
    return lesson_id

# --- MAIN GENERATION FUNCTION (PDF + HTML SUPPORT) ---
def generate_and_save_quiz(
    file_input: str,
    quiz_title: Optional[str] = None,
    api_key: Optional[str] = None,
    teacher_id: int = 1,
    num_mcq: int = 3,
    num_written: int = 2
):
    if not os.path.exists(file_input):
        print(f"❌ Алдаа: '{file_input}' файл олдсонгүй.")
        sys.exit(1)

    file_ext = os.path.splitext(file_input)[1].lower()
    if file_ext not in [".pdf", ".html", ".htm"]:
        print(f"❌ Алдаа: Зөвхөн .pdf болон .html файлууд дэмжигдэнэ (Өгөгдсөн: {file_ext})")
        sys.exit(1)

    # Гарчиг тохируулах
    if not quiz_title:
        base_name = os.path.splitext(os.path.basename(file_input))[0]
        quiz_title = f"{base_name.capitalize()} - IB Үнэлгээний Сорил"

    resolved_key = resolve_api_key(api_key)
    print("🔑 Gemini API түлхүүр баталгаажлаа.")

    # DB-д бүртгэх
    lesson_id = ensure_lesson_in_db(file_input, quiz_title, teacher_id)
    client = genai.Client(api_key=resolved_key)

    prompt_instruction = f"""
    Та бол олон улсын бакалавр (IB - International Baccalaureate) болон Бизнес, ERP, Санхүүгийн мэргэжлийн багш.
    Өгөгдсөн хичээлийн материалын агуулгыг үндэслэн 10-р ангийн сурагчдад зориулсан шалгалт үүсгэнэ үү.

    ШАЛГАЛТЫН ШААРДЛАГА:
    1. Нийт {num_mcq} сонгох асуулт (multiple_choice), тус бүр 4 сонголттой ба зөв хариултын индексийг (0-3) заана.
    2. Нийт {num_written} задгай бичих асуулт (written_response) үүсгэнэ.
    3. Асуултууд нь материал дээрх гол ойлголтууд, тооцоолол, кейс, дебит/кредит, татвар (НӨАТ, ААННОАТ), нийгмийн даатгал, үйл ажиллагааны логикийг шалгана.
    4. Асуулт бүрийг IB MYP/DP үнэлгээний шалгуурт (Criterion A, Criterion B, Criterion C, эсвэл Criterion D) заавал оноож, rubric_guidance талбарт сурагчаас юу шаардахыг бичнэ үү.
    """

    contents = []
    if file_ext == ".pdf":
        print(f"📄 PDF файлыг Gemini руу шууд илгээж байна (Зураг, хүснэгтийг бүрэн танина): {file_input}...")
        uploaded_pdf = client.files.upload(file=file_input)
        contents = [uploaded_pdf, prompt_instruction]
    else:
        print(f"📄 HTML файлыг уншиж байна: {file_input}...")
        extracted_text = extract_content_from_html(file_input)
        contents = [f"{prompt_instruction}\n\nСЛАЙДЫН АГУУЛГА:\n{extracted_text}"]

    print("🤖 Gemini 2.5 Flash ажиллаж байна...")
    try:
        response = client.models.generate_content(
            # model="gemini-2.5-flash",
            model="gemini-3.8-flash",
            contents=contents,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=GeneratedQuiz,
                temperature=0.2
            ),
        )
        parsed_quiz = json.loads(response.text)
    except Exception as e:
        print(f"❌ Gemini generation алдаа гарлаа: {e}")
        sys.exit(1)

    # Өгөгдлийн санд хадгалах
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "INSERT INTO quizzes (lesson_id, title, questions_json, image_path, created_by) VALUES (?, ?, ?, ?, ?)",
        (lesson_id, quiz_title, json.dumps(parsed_quiz), None, teacher_id)
    )
    conn.commit()
    quiz_id = c.lastrowid
    conn.close()

    print("\n" + "="*50)
    print(f"✅ АМЖИЛТТАЙ! Шалгалт системд хадгалагдлаа.")
    print(f"📌 Шалгалтын ID: {quiz_id}")
    print(f"📌 Шалгалтын нэр: {quiz_title}")
    print(f"📌 Асуултын тоо: {len(parsed_quiz['questions'])} ({num_mcq} сонгох, {num_written} задгай)")
    print(f"🌐 Вэб линк: http://localhost:8000/quiz/take/{quiz_id}")
    print("="*50)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate IB Quiz from PDF or HTML Presentation using Gemini")
    parser.add_argument("--file_input", "--html_file_input", dest="file_input", type=str, required=True, 
                        help="Path to .pdf or .html presentation file")
    parser.add_argument("--quiz_title", type=str, default=None, help="Custom Title for the quiz")
    parser.add_argument("--api_key", type=str, default=None, help="Gemini API Key (optional if saved in Web App)")
    parser.add_argument("--teacher_id", type=int, default=1, help="Teacher User ID (default: 1)")
    parser.add_argument("--num_mcq", type=int, default=3, help="Number of Multiple Choice Questions (default: 3)")
    parser.add_argument("--num_written", type=int, default=2, help="Number of Written Open Questions (default: 2)")

    args = parser.parse_args()
    generate_and_save_quiz(
        file_input=args.file_input,
        quiz_title=args.quiz_title,
        api_key=args.api_key,
        teacher_id=args.teacher_id,
        num_mcq=args.num_mcq,
        num_written=args.num_written
    )
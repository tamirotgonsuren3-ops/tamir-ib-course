import os
import console_utils  # noqa: F401 - configures UTF-8 console output on Windows
import sys
import json
import sqlite3
import argparse
from typing import Optional
from html.parser import HTMLParser
from quiz_engine import generate_quiz

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "edupath.db")
UPLOAD_LESSONS = os.path.join(BASE_DIR, "uploads", "lessons")
os.makedirs(UPLOAD_LESSONS, exist_ok=True)

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

def resolve_api_key(provider: str, cli_key: Optional[str], teacher_id: int) -> Optional[str]:
    if provider == "ollama":
        return None
    if cli_key and cli_key.strip():
        return cli_key.strip()

    env_name = "DEEPSEEK_API_KEY" if provider == "deepseek" else "GEMINI_API_KEY"
    env_key = os.environ.get(env_name)
    if env_key:
        return env_key.strip()

    if os.path.exists(DB_PATH):
        conn = get_db_connection()
        c = conn.cursor()
        key_column = "deepseek_api_key" if provider == "deepseek" else "gemini_api_key"
        columns = {row[1] for row in c.execute("PRAGMA table_info(users)").fetchall()}
        row = None
        if key_column in columns:
            c.execute(
                f"SELECT {key_column} FROM users WHERE id = ? AND role = 'teacher'",
                (teacher_id,)
            )
            row = c.fetchone()
        conn.close()
        if row and row[0]:
            return row[0]

    print(f"❌ Алдаа: {provider} API key олдсонгүй!")
    print("Шийдэл: --api_key, provider-ийн environment variable, эсвэл веб тохиргоонд түлхүүрээ оруулна уу.")
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
    provider: str = "gemini",
    model: Optional[str] = None,
    prompt: str = "",
):
    if not os.path.exists(file_input):
        print(f"❌ Алдаа: '{file_input}' файл олдсонгүй.")
        sys.exit(1)

    file_ext = os.path.splitext(file_input)[1].lower()
    if file_ext not in [".pdf", ".html", ".htm"]:
        print(f"❌ Алдаа: Зөвхөн .pdf болон .html файлууд дэмжигдэнэ (Өгөгдсөн: {file_ext})")
        sys.exit(1)
    if file_ext == ".pdf" and provider != "gemini":
        print("❌ Алдаа: PDF оролт одоогоор зөвхөн Gemini provider-той ажиллана.")
        sys.exit(1)

    # Гарчиг тохируулах
    if not quiz_title:
        base_name = os.path.splitext(os.path.basename(file_input))[0]
        quiz_title = f"{base_name.capitalize()} - IB Үнэлгээний Сорил"

    resolved_key = resolve_api_key(provider, api_key, teacher_id)
    print(f"🔑 {provider} provider сонгогдлоо.")

    # DB-д бүртгэх
    lesson_id = ensure_lesson_in_db(file_input, quiz_title, teacher_id)
    if file_ext == ".pdf":
        print(f"📄 PDF материал ашиглаж байна: {file_input}...")
        lesson_content = "PDF хичээлийн материалыг хавсралтаас ашиглана уу."
        pdf_path = file_input
    else:
        print(f"📄 HTML файлыг уншиж байна: {file_input}...")
        lesson_content = extract_content_from_html(file_input)
        pdf_path = None

    full_prompt = "\n\n".join(part for part in (prompt.strip(), lesson_content) if part)
    print(f"🤖 {provider} ашиглан 8 асуулт үүсгэж байна...")
    try:
        parsed_quiz = generate_quiz(
            provider=provider,
            prompt=full_prompt,
            title=quiz_title,
            api_key=resolved_key,
            model=model,
            pdf_path=pdf_path,
        )
    except Exception as e:
        print(f"❌ {provider} generation алдаа гарлаа: {e}")
        sys.exit(1)

    # Өгөгдлийн санд хадгалах
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "INSERT INTO quizzes (lesson_id, title, questions_json, image_path, created_by) VALUES (?, ?, ?, ?, ?)",
        (lesson_id, quiz_title, json.dumps(parsed_quiz.model_dump()), None, teacher_id)
    )
    conn.commit()
    quiz_id = c.lastrowid
    conn.close()

    print("\n" + "="*50)
    print(f"✅ АМЖИЛТТАЙ! Шалгалт системд хадгалагдлаа.")
    print(f"📌 Шалгалтын ID: {quiz_id}")
    print(f"📌 Шалгалтын нэр: {quiz_title}")
    print(f"📌 Асуултын тоо: {len(parsed_quiz.questions)} (шалгуур тус бүрт 2)")
    print(f"🌐 Вэб линк: http://localhost:8000/quiz/take/{quiz_id}")
    print("="*50)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate a fixed-format IB quiz using Gemini, DeepSeek, or Ollama")
    parser.add_argument("--file_input", "--html_file_input", dest="file_input", type=str, required=True, 
                        help="Path to .pdf or .html presentation file")
    parser.add_argument("--quiz_title", type=str, default=None, help="Custom Title for the quiz")
    parser.add_argument("--provider", choices=("gemini", "deepseek", "ollama"), default="gemini")
    parser.add_argument("--model", type=str, default=None, help="Optional provider model override")
    parser.add_argument("--prompt", type=str, default="", help="Additional instructions or topic context")
    parser.add_argument("--api_key", type=str, default=None, help="Provider API key (optional if saved or set in environment)")
    parser.add_argument("--teacher_id", type=int, default=1, help="Teacher User ID (default: 1)")

    args = parser.parse_args()
    generate_and_save_quiz(
        file_input=args.file_input,
        quiz_title=args.quiz_title,
        api_key=args.api_key,
        teacher_id=args.teacher_id,
        provider=args.provider,
        model=args.model,
        prompt=args.prompt,
    )
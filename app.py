import os
import console_utils  # noqa: F401 - configures UTF-8 console output on Windows
import json
import re
import sqlite3
import shutil
import threading
import time
import uuid
from datetime import datetime, date
from typing import Optional
from fastapi import FastAPI, Request, Form, File, UploadFile, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from quiz_engine import generate_quiz, score_quiz
from homework_engine import (
    grade_submission,
    grade_fixed_answer,
    clamp_score,
    DEFAULT_MAX_SCORE,
)

app = FastAPI(title="EduPath IB & Odoo ERP Learning Platform")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_LESSONS = os.path.join(BASE_DIR, "uploads", "lessons")
UPLOAD_IMAGES = os.path.join(BASE_DIR, "uploads", "quiz_images")
UPLOAD_PHOTOS = os.path.join(BASE_DIR, "uploads", "photos")
os.makedirs(UPLOAD_LESSONS, exist_ok=True)
os.makedirs(UPLOAD_IMAGES, exist_ok=True)
os.makedirs(UPLOAD_PHOTOS, exist_ok=True)

templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

GRADE_OPTIONS = [f"{g}{s}" for g in range(1, 13) for s in ("a", "b", "c")]


def parse_grade(grade: str):
    m = re.fullmatch(r"(\d{1,2})([abc])", (grade or "").strip().lower())
    if not m:
        return None
    n = int(m.group(1))
    if n < 1 or n > 12:
        return None
    return (n, m.group(2))


def ib_to_100(total_score) -> float:
    try:
        return round(float(total_score) * 12.5, 1)
    except (TypeError, ValueError):
        return 0.0


def ts_to_datetime(value):
    try:
        return datetime.fromtimestamp(int(value)).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return ""


templates.env.globals["ib_to_100"] = ib_to_100
templates.env.globals["grade_options"] = GRADE_OPTIONS
templates.env.filters["datetime"] = ts_to_datetime

# --- DATABASE SETUP (SQLite) ---
DB_PATH = os.path.join(BASE_DIR, "edupath.db")


def _ensure_columns(c: sqlite3.Cursor, table: str, columns: dict[str, str]):
    existing = {row[1] for row in c.execute(f"PRAGMA table_info({table})").fetchall()}
    for name, col_def in columns.items():
        if name not in existing:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {col_def}")


def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # Хэрэглэгчид (Багш / Сурагч)
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE,
        password TEXT,
        role TEXT,
        gemini_api_key TEXT,
        deepseek_api_key TEXT
    )''')
    _ensure_columns(c, "users", {
        "deepseek_api_key": "deepseek_api_key TEXT",
        "grade": "grade TEXT",
        "name": "name TEXT",
        "surname": "surname TEXT",
        "student_code": "student_code TEXT",
        "email": "email TEXT",
        "gender": "gender TEXT",
        "phone": "phone TEXT",
        "instagram": "instagram TEXT",
        "photo_path": "photo_path TEXT",
    })
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_student_code ON users(student_code) WHERE student_code IS NOT NULL")
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users(email) WHERE email IS NOT NULL")
    # Хичээлүүд (lesson.html)
    c.execute('''CREATE TABLE IF NOT EXISTS lessons (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT,
        filename TEXT,
        teacher_id INTEGER
    )''')
    # Шалгалтууд
    c.execute('''CREATE TABLE IF NOT EXISTS quizzes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        lesson_id INTEGER,
        title TEXT,
        questions_json TEXT,
        image_path TEXT,
        created_by INTEGER
    )''')
    _ensure_columns(c, "quizzes", {
        "is_active": "is_active INTEGER DEFAULT 1",
        "time_limit_minutes": "time_limit_minutes INTEGER",
    })
    c.execute("UPDATE quizzes SET is_active = 1 WHERE is_active IS NULL")
    c.execute('''CREATE TABLE IF NOT EXISTS quiz_attempts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        quiz_id INTEGER NOT NULL,
        student_id INTEGER NOT NULL,
        started_at INTEGER NOT NULL,
        submitted_at INTEGER,
        UNIQUE(quiz_id, student_id)
    )''')
    # Сурагчийн хариултууд ба IB үнэлгээ
    c.execute('''CREATE TABLE IF NOT EXISTS submissions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        quiz_id INTEGER,
        student_id INTEGER,
        responses_json TEXT,
        evaluation_json TEXT,
        total_score INTEGER
    )''')
    # Гэрийн даалгавар
    c.execute('''CREATE TABLE IF NOT EXISTS homework (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        description TEXT,
        starter_code TEXT,
        max_score INTEGER DEFAULT 10,
        due_date TEXT,
        grade TEXT,
        check_mode TEXT DEFAULT 'recursive',
        check_type TEXT DEFAULT 'tests',
        fixed_answer TEXT,
        created_by INTEGER,
        created_at INTEGER,
        is_active INTEGER DEFAULT 1
    )''')
    _ensure_columns(c, "homework", {
        "check_type": "check_type TEXT DEFAULT 'tests'",
        "fixed_answer": "fixed_answer TEXT",
    })
    c.execute("UPDATE homework SET check_type = 'tests' WHERE check_type IS NULL OR check_type = ''")
    c.execute('''CREATE TABLE IF NOT EXISTS homework_tests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        homework_id INTEGER NOT NULL,
        name TEXT,
        test_code TEXT NOT NULL
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS homework_submissions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        homework_id INTEGER NOT NULL,
        student_id INTEGER NOT NULL,
        code TEXT,
        status TEXT DEFAULT 'pending',
        score INTEGER,
        max_score INTEGER,
        explanation TEXT,
        test_results_json TEXT,
        submitted_at INTEGER,
        checked_at INTEGER,
        UNIQUE(homework_id, student_id)
    )''')
    # Ирц, сахилга бат, урамшуулал
    c.execute('''CREATE TABLE IF NOT EXISTS attendance_config (
        grade TEXT PRIMARY KEY,
        present_score INTEGER DEFAULT 2,
        late_score INTEGER DEFAULT 1,
        absent_score INTEGER DEFAULT 0,
        updated_at INTEGER
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS attendance_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        student_id INTEGER NOT NULL,
        grade TEXT,
        date TEXT NOT NULL,
        status TEXT NOT NULL,
        score INTEGER DEFAULT 0,
        note TEXT,
        created_by INTEGER,
        created_at INTEGER,
        UNIQUE(student_id, date)
    )''')
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_attendance_date_grade ON attendance_records(date, grade)"
    )
    c.execute('''CREATE TABLE IF NOT EXISTS discipline_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        student_id INTEGER NOT NULL,
        grade TEXT,
        date TEXT NOT NULL,
        note TEXT,
        score_delta INTEGER NOT NULL DEFAULT 0,
        source TEXT DEFAULT 'manual',
        created_by INTEGER,
        created_at INTEGER
    )''')
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_discipline_student_date ON discipline_records(student_id, date)"
    )
    conn.commit()
    conn.close()

init_db()

def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()

# --- AUTH HELPERS (Cookie-based session) ---
def get_current_user(request: Request, db: sqlite3.Connection = Depends(get_db)):
    user_id = request.cookies.get("user_id")
    if not user_id:
        return None
    c = db.cursor()
    c.execute("SELECT * FROM users WHERE id = ?", (user_id,))
    return c.fetchone()

# --- ROUTES ---

@app.get("/", response_class=HTMLResponse)
def index(request: Request, user: dict = Depends(get_current_user)):
    if not user:
        return RedirectResponse(url="/login")
    if user["role"] == "teacher":
        return RedirectResponse(url="/teacher")
    return RedirectResponse(url="/student")

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(request=request, name="login.html")

@app.post("/register")
async def register(
    grade: str = Form(...),
    name: str = Form(...),
    surname: str = Form(...),
    student_code: str = Form(...),
    email: str = Form(...),
    gender: str = Form(...),
    phone: str = Form(...),
    instagram: str = Form(""),
    password: str = Form(...),
    photo: UploadFile = File(...),
    db: sqlite3.Connection = Depends(get_db),
):
    grade_norm = (grade or "").strip().lower()
    if not parse_grade(grade_norm):
        return RedirectResponse(url="/login?error=invalid_grade", status_code=status.HTTP_302_FOUND)
    name = name.strip()
    surname = surname.strip()
    student_code = student_code.strip()
    email = email.strip().lower()
    gender = gender.strip()
    phone = phone.strip()
    instagram = (instagram or "").strip()
    if not all([name, surname, student_code, email, gender, phone, password]):
        return RedirectResponse(url="/login?error=missing_fields", status_code=status.HTTP_302_FOUND)
    if not photo or not photo.filename:
        return RedirectResponse(url="/login?error=photo_required", status_code=status.HTTP_302_FOUND)

    ext = os.path.splitext(photo.filename)[1].lower()
    if ext not in {".jpg", ".jpeg", ".png", ".webp"}:
        return RedirectResponse(url="/login?error=invalid_photo", status_code=status.HTTP_302_FOUND)
    safe_photo = f"{uuid.uuid4().hex}{ext}"
    photo_path = os.path.join(UPLOAD_PHOTOS, safe_photo)
    with open(photo_path, "wb") as buf:
        shutil.copyfileobj(photo.file, buf)

    c = db.cursor()
    try:
        c.execute(
            """INSERT INTO users (username, password, role, grade, name, surname, student_code, email, gender, phone, instagram, photo_path)
               VALUES (?, ?, 'student', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (student_code, password, grade_norm, name, surname, student_code, email, gender, phone, instagram or None, safe_photo),
        )
        db.commit()
    except sqlite3.IntegrityError:
        try:
            os.remove(photo_path)
        except OSError:
            pass
        return RedirectResponse(url="/login?error=exists", status_code=status.HTTP_302_FOUND)
    return RedirectResponse(url="/login?registered=1", status_code=status.HTTP_302_FOUND)


@app.post("/register/teacher")
def register_teacher(
    username: str = Form(...),
    password: str = Form(...),
    db: sqlite3.Connection = Depends(get_db),
):
    c = db.cursor()
    try:
        c.execute("INSERT INTO users (username, password, role) VALUES (?, ?, 'teacher')", (username.strip(), password))
        db.commit()
    except sqlite3.IntegrityError:
        return RedirectResponse(url="/login?error=exists", status_code=status.HTTP_302_FOUND)
    return RedirectResponse(url="/login?registered=1", status_code=status.HTTP_302_FOUND)

@app.post("/login")
def login(
    username: str = Form(...),
    password: str = Form(...),
    db: sqlite3.Connection = Depends(get_db)
):
    c = db.cursor()
    c.execute("SELECT * FROM users WHERE username = ? AND password = ?", (username, password))
    user = c.fetchone()
    if not user:
        return RedirectResponse(url="/login?error=1", status_code=status.HTTP_302_FOUND)
    response = RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
    response.set_cookie(key="user_id", value=str(user["id"]), httponly=True)
    return response

@app.get("/logout")
def logout():
    response = RedirectResponse(url="/login")
    response.delete_cookie("user_id")
    return response

# --- TEACHER DASHBOARD ---

@app.get("/teacher", response_class=HTMLResponse)
def teacher_dashboard(request: Request, user: dict = Depends(get_current_user), db: sqlite3.Connection = Depends(get_db)):
    if not user or user["role"] != "teacher":
        return RedirectResponse("/login")
    c = db.cursor()
    c.execute("SELECT * FROM lessons WHERE teacher_id = ?", (user["id"],))
    lessons = c.fetchall()
    c.execute("SELECT * FROM lessons ORDER BY id DESC")
    published_lessons = c.fetchall()
    c.execute("SELECT quizzes.*, lessons.title as lesson_title, COALESCE(quizzes.is_active, 1) as is_active FROM quizzes JOIN lessons ON quizzes.lesson_id = lessons.id ORDER BY quizzes.id DESC")
    quizzes = c.fetchall()
    # return templates.TemplateResponse( request=request, name="teacher.html")
    return templates.TemplateResponse(
        request=request, 
        name="teacher.html", 
        context={
            "user": user,
            "lessons": lessons,
            "published_lessons": published_lessons,
            "quizzes": quizzes
        }
    )


@app.post("/teacher/api-key")
def update_api_key(
    api_key: str = Form(""),
    deepseek_api_key: str = Form(""),
    user: dict = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db)
):
    if not user or user["role"] != "teacher":
        raise HTTPException(status_code=403)
    c = db.cursor()
    c.execute(
        "UPDATE users SET gemini_api_key = ?, deepseek_api_key = ? WHERE id = ?",
        (api_key.strip(), deepseek_api_key.strip(), user["id"])
    )
    db.commit()
    return RedirectResponse("/teacher?apikey_updated=1", status_code=status.HTTP_302_FOUND)

@app.post("/teacher/upload-lesson")
async def upload_lesson(
    title: str = Form(...),
    file: UploadFile = File(...),
    user: dict = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db)
):
    if not user or user["role"] != "teacher":
        raise HTTPException(status_code=403)
    
    original_filename = os.path.basename((file.filename or "").replace("\\", "/"))
    extension = os.path.splitext(original_filename)[1].lower()
    if extension not in {".html", ".htm"}:
        raise HTTPException(status_code=400, detail="Зөвхөн .html эсвэл .htm файл оруулна уу.")

    safe_filename = f"{user['id']}_{uuid.uuid4().hex}{extension}"
    file_path = os.path.join(UPLOAD_LESSONS, safe_filename)
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
    
    c = db.cursor()
    c.execute("INSERT INTO lessons (title, filename, teacher_id) VALUES (?, ?, ?)", (title, safe_filename, user["id"]))
    db.commit()
    return RedirectResponse("/teacher?lesson_uploaded=1", status_code=status.HTTP_302_FOUND)

@app.post("/teacher/create-quiz")
def create_quiz_with_gemini(
    lesson_id: int = Form(...),
    quiz_title: str = Form(...),
    prompt: str = Form(...),
    provider: str = Form("gemini"),
    model: str = Form(""),
    time_limit_minutes: Optional[str] = Form(None),
    image: Optional[UploadFile] = File(None),
    user: dict = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db)
):
    if not user or user["role"] != "teacher":
        raise HTTPException(status_code=403)
    provider_keys = {
        "gemini": user["gemini_api_key"],
        "deepseek": user["deepseek_api_key"],
        "ollama": None,
    }
    if provider not in provider_keys:
        return RedirectResponse("/teacher?error=unsupported_provider", status_code=status.HTTP_302_FOUND)
    if provider != "ollama" and not provider_keys[provider]:
        return RedirectResponse(f"/teacher?error=missing_{provider}_key", status_code=status.HTTP_302_FOUND)
    if image and image.filename and provider != "gemini":
        return RedirectResponse("/teacher?error=image_requires_gemini", status_code=status.HTTP_302_FOUND)

    image_saved_path = None
    image_path = None
    if image and image.filename:
        extension = os.path.splitext(image.filename)[1].lower()
        safe_img_name = f"quiz_{user['id']}_{uuid.uuid4().hex}{extension}"
        img_dest = os.path.join(UPLOAD_IMAGES, safe_img_name)
        with open(img_dest, "wb") as buf:
            shutil.copyfileobj(image.file, buf)
        image_saved_path = safe_img_name
        image_path = img_dest

    try:
        parsed_quiz = generate_quiz(
            provider=provider,
            prompt=prompt,
            title=quiz_title,
            api_key=provider_keys[provider],
            model=model.strip() or None,
            image_path=image_path,
        )
    except Exception as e:
        print(f"Quiz generation error ({provider}):", e)
        return RedirectResponse("/teacher?error=generation_failed", status_code=status.HTTP_302_FOUND)

    tl = None
    if time_limit_minutes is not None and str(time_limit_minutes).strip():
        try:
            tl = int(str(time_limit_minutes).strip())
            if tl < 1 or tl > 600:
                tl = None
        except ValueError:
            tl = None

    c = db.cursor()
    c.execute(
        "INSERT INTO quizzes (lesson_id, title, questions_json, image_path, created_by, is_active, time_limit_minutes) VALUES (?, ?, ?, ?, ?, 1, ?)",
        (lesson_id, quiz_title, json.dumps(parsed_quiz.model_dump()), image_saved_path, user["id"], tl)
    )
    db.commit()
    return RedirectResponse("/teacher?quiz_created=1", status_code=status.HTTP_302_FOUND)


@app.post("/teacher/quiz/{quiz_id}/toggle")
def toggle_quiz(
    quiz_id: int,
    user: dict = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    if not user or user["role"] != "teacher":
        raise HTTPException(status_code=403)
    c = db.cursor()
    c.execute("SELECT COALESCE(is_active, 1) FROM quizzes WHERE id = ?", (quiz_id,))
    row = c.fetchone()
    if not row:
        raise HTTPException(status_code=404)
    new_state = 0 if row[0] else 1
    c.execute("UPDATE quizzes SET is_active = ? WHERE id = ?", (new_state, quiz_id))
    db.commit()
    return RedirectResponse("/teacher", status_code=status.HTTP_302_FOUND)


@app.get("/scores", response_class=HTMLResponse)
def scores_page(request: Request, user: dict = Depends(get_current_user), db: sqlite3.Connection = Depends(get_db)):
    if not user or user["role"] != "teacher":
        return RedirectResponse("/login")
    c = db.cursor()
    c.execute(
        """
        SELECT s.id, s.total_score, s.evaluation_json, s.responses_json,
               q.title as quiz_title, q.id as quiz_id,
               u.id as student_id, u.username, u.name, u.surname, u.grade, u.student_code, u.email, u.photo_path
        FROM submissions s
        JOIN quizzes q ON s.quiz_id = q.id
        JOIN users u ON s.student_id = u.id
        WHERE u.role = 'student'
        ORDER BY u.grade, u.surname, u.name, s.id DESC
        """
    )
    rows = []
    for r in c.fetchall():
        d = dict(r)
        try:
            d["evaluation"] = json.loads(d["evaluation_json"]) if d["evaluation_json"] else {}
        except Exception:
            d["evaluation"] = {}
        d["score_100"] = ib_to_100(d["total_score"])
        rows.append(d)
    c.execute(
        """
        SELECT id, username, name, surname, grade, student_code, email, gender, phone, instagram, photo_path
        FROM users WHERE role = 'student'
        ORDER BY grade, surname, name
        """
    )
    students = [dict(r) for r in c.fetchall()]
    return templates.TemplateResponse(
        request=request,
        name="scores.html",
        context={"user": user, "rows": rows, "students": students},
    )


# --- HOMEWORK HELPERS ---

def _get_homework(db: sqlite3.Connection, homework_id: int):
    c = db.cursor()
    c.execute("SELECT * FROM homework WHERE id = ?", (homework_id,))
    return c.fetchone()


def _student_can_view_homework(homework, user: dict) -> bool:
    if not homework or not homework["is_active"]:
        return False
    target_grade = (homework["grade"] or "").strip().lower()
    if target_grade and target_grade != (user["grade"] or "").strip().lower():
        return False
    return True


# --- ATTENDANCE / DISCIPLINE HELPERS ---

DEFAULT_ATTENDANCE_CONFIG = {
    "present_score": 2,
    "late_score": 1,
    "absent_score": 0,
}


def _today_str() -> str:
    return date.today().isoformat()


def _normalize_date(value: Optional[str]) -> Optional[str]:
    try:
        return datetime.strptime(str(value or "").strip(), "%Y-%m-%d").date().isoformat()
    except (TypeError, ValueError):
        return None


def _grades_with_students(db: sqlite3.Connection) -> list[str]:
    c = db.cursor()
    c.execute(
        "SELECT DISTINCT grade FROM users WHERE role = 'student' AND grade IS NOT NULL AND grade <> ''"
    )
    grades = [row[0] for row in c.fetchall()]
    return sorted(grades, key=lambda grade: parse_grade(grade) or (99, grade))


def _resolve_grade(db: sqlite3.Connection, grade: Optional[str]) -> str:
    grades = _grades_with_students(db)
    requested = (grade or "").strip().lower()
    if requested in grades:
        return requested
    return grades[0] if grades else ""


def _teacher_students(db: sqlite3.Connection, grade: str) -> list[dict]:
    c = db.cursor()
    c.execute(
        "SELECT * FROM users WHERE role = 'student' AND grade = ? ORDER BY surname, name",
        (grade,),
    )
    return [dict(row) for row in c.fetchall()]


def _get_attendance_config(db: sqlite3.Connection, grade: str) -> dict:
    c = db.cursor()
    c.execute("SELECT * FROM attendance_config WHERE grade = ?", (grade,))
    row = c.fetchone()
    if row:
        return dict(row)
    return {"grade": grade, **DEFAULT_ATTENDANCE_CONFIG}


def _clamp_delta(value, low: int = -100, high: int = 100) -> int:
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        return 0
    return max(low, min(high, number))


def _student_score_summary(db: sqlite3.Connection, student_id: int) -> dict:
    c = db.cursor()
    c.execute(
        "SELECT COALESCE(SUM(score), 0) FROM homework_submissions WHERE student_id = ? AND status = 'done'",
        (student_id,),
    )
    homework_total = c.fetchone()[0] or 0
    c.execute(
        "SELECT COALESCE(SUM(score), 0) FROM attendance_records WHERE student_id = ?",
        (student_id,),
    )
    attendance_total = c.fetchone()[0] or 0
    c.execute(
        "SELECT COALESCE(SUM(score_delta), 0) FROM discipline_records WHERE student_id = ?",
        (student_id,),
    )
    discipline_total = c.fetchone()[0] or 0
    return {
        "homework": int(homework_total),
        "attendance": int(attendance_total),
        "discipline": int(discipline_total),
        "total": int(homework_total) + int(attendance_total) + int(discipline_total),
    }


def _grade_and_store(db: sqlite3.Connection, submission_id: int):
    c = db.cursor()
    c.execute(
        """
        SELECT hs.*, h.description, h.max_score as homework_max_score,
               h.check_type, h.fixed_answer
        FROM homework_submissions hs
        JOIN homework h ON h.id = hs.homework_id
        WHERE hs.id = ?
        """,
        (submission_id,),
    )
    sub = c.fetchone()
    if not sub:
        return
    c.execute(
        "SELECT name, test_code FROM homework_tests WHERE homework_id = ? ORDER BY id",
        (sub["homework_id"],),
    )
    tests = [dict(row) for row in c.fetchall()]
    max_score = int(sub["homework_max_score"] or sub["max_score"] or DEFAULT_MAX_SCORE)
    if (sub["check_type"] or "tests") == "fixed":
        result = grade_fixed_answer(
            answer=sub["code"] or "",
            expected=sub["fixed_answer"] or "",
            max_score=max_score,
            description=sub["description"] or "",
        )
    else:
        result = grade_submission(
            code=sub["code"] or "",
            tests=tests,
            description=sub["description"] or "",
            max_score=max_score,
            use_ai=True,
        )
    c.execute(
        """UPDATE homework_submissions
           SET status = ?, score = ?, max_score = ?, explanation = ?, test_results_json = ?, checked_at = ?
           WHERE id = ?""",
        (
            result["status"],
            result["score"],
            max_score,
            result["explanation"],
            json.dumps(result.get("test_results") or {}, ensure_ascii=False),
            int(time.time()),
            submission_id,
        ),
    )
    db.commit()


def _grade_in_background(submission_id: int):
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        _grade_and_store(conn, submission_id)
    except Exception as exc:
        try:
            conn.execute(
                "UPDATE homework_submissions SET status = 'error', explanation = ?, checked_at = ? WHERE id = ?",
                (f"Шалгах үед алдаа гарлаа: {exc}", int(time.time()), submission_id),
            )
            conn.commit()
        except Exception:
            pass
    finally:
        conn.close()


def _parse_homework_form(form) -> tuple[Optional[dict], Optional[str]]:
    title = str(form.get("title") or "").strip()
    if not title:
        return None, "title"
    try:
        max_score = int(str(form.get("max_score") or DEFAULT_MAX_SCORE).strip())
    except ValueError:
        max_score = DEFAULT_MAX_SCORE
    max_score = max(1, min(100, max_score))
    check_mode = str(form.get("check_mode") or "recursive").strip().lower()
    if check_mode not in {"recursive", "queue"}:
        check_mode = "recursive"
    check_type = str(form.get("check_type") or "tests").strip().lower()
    if check_type not in {"tests", "fixed"}:
        check_type = "tests"
    fixed_answer = str(form.get("fixed_answer") or "").strip()
    if check_type == "fixed" and not fixed_answer:
        return None, "fixed"
    if check_type != "fixed":
        fixed_answer = None
    return {
        "title": title,
        "description": str(form.get("description") or "").strip(),
        "starter_code": str(form.get("starter_code") or ""),
        "max_score": max_score,
        "due_date": str(form.get("due_date") or "").strip() or None,
        "grade": str(form.get("grade") or "").strip().lower() or None,
        "check_mode": check_mode,
        "check_type": check_type,
        "fixed_answer": fixed_answer,
    }, None


def _save_homework_tests(c: sqlite3.Cursor, homework_id: int, form) -> None:
    c.execute("DELETE FROM homework_tests WHERE homework_id = ?", (homework_id,))
    names = form.getlist("test_name")
    codes = form.getlist("test_code")
    for index, raw_code in enumerate(codes, start=1):
        test_code = str(raw_code or "").strip()
        if not test_code:
            continue
        raw_name = names[index - 1] if index - 1 < len(names) else ""
        test_name = str(raw_name or "").strip() or f"Тест {index}"
        c.execute(
            "INSERT INTO homework_tests (homework_id, name, test_code) VALUES (?, ?, ?)",
            (homework_id, test_name, test_code),
        )


# --- TEACHER HOMEWORK ---

@app.get("/teacher/homework", response_class=HTMLResponse)
def teacher_homework_page(
    request: Request,
    user: dict = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    if not user or user["role"] != "teacher":
        return RedirectResponse("/login")
    c = db.cursor()
    c.execute(
        """
        SELECT h.*,
               (SELECT COUNT(*) FROM homework_submissions hs WHERE hs.homework_id = h.id) AS submission_count,
               (SELECT COUNT(*) FROM homework_submissions hs WHERE hs.homework_id = h.id AND hs.status = 'done') AS graded_count
        FROM homework h
        WHERE h.created_by = ?
        ORDER BY h.id DESC
        """,
        (user["id"],),
    )
    homeworks = [dict(row) for row in c.fetchall()]
    return templates.TemplateResponse(
        request=request,
        name="teacher_homework.html",
        context={"user": user, "homeworks": homeworks},
    )


@app.get("/teacher/homework/new", response_class=HTMLResponse)
def teacher_homework_new_page(
    request: Request,
    user: dict = Depends(get_current_user),
):
    if not user or user["role"] != "teacher":
        return RedirectResponse("/login")
    return templates.TemplateResponse(
        request=request,
        name="teacher_homework_new.html",
        context={"user": user},
    )


@app.post("/teacher/homework/new")
async def teacher_homework_create(
    request: Request,
    user: dict = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    if not user or user["role"] != "teacher":
        raise HTTPException(status_code=403)
    form = await request.form()
    data, error = _parse_homework_form(form)
    if error:
        return RedirectResponse(f"/teacher/homework/new?error={error}", status_code=status.HTTP_302_FOUND)

    c = db.cursor()
    c.execute(
        """INSERT INTO homework
           (title, description, starter_code, max_score, due_date, grade, check_mode, check_type, fixed_answer,
            created_by, created_at, is_active)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
        (
            data["title"],
            data["description"],
            data["starter_code"],
            data["max_score"],
            data["due_date"],
            data["grade"],
            data["check_mode"],
            data["check_type"],
            data["fixed_answer"],
            user["id"],
            int(time.time()),
        ),
    )
    homework_id = c.lastrowid
    if data["check_type"] == "tests":
        _save_homework_tests(c, homework_id, form)
    db.commit()
    return RedirectResponse(f"/teacher/homework/{homework_id}?created=1", status_code=status.HTTP_302_FOUND)


@app.get("/teacher/homework/{homework_id}", response_class=HTMLResponse)
def teacher_homework_detail(
    homework_id: int,
    request: Request,
    user: dict = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    if not user or user["role"] != "teacher":
        return RedirectResponse("/login")
    homework = _get_homework(db, homework_id)
    if not homework or homework["created_by"] != user["id"]:
        raise HTTPException(status_code=404)
    c = db.cursor()
    c.execute("SELECT * FROM homework_tests WHERE homework_id = ? ORDER BY id", (homework_id,))
    tests = [dict(row) for row in c.fetchall()]
    c.execute(
        """
        SELECT hs.*, u.name, u.surname, u.username, u.grade, u.photo_path
        FROM homework_submissions hs
        JOIN users u ON u.id = hs.student_id
        WHERE hs.homework_id = ?
        ORDER BY u.surname, u.name
        """,
        (homework_id,),
    )
    submissions = [dict(row) for row in c.fetchall()]
    sub_map = {row["student_id"]: row for row in submissions}

    if (homework["grade"] or "").strip():
        c.execute(
            "SELECT * FROM users WHERE role = 'student' AND grade = ? ORDER BY surname, name",
            (homework["grade"],),
        )
    else:
        c.execute("SELECT * FROM users WHERE role = 'student' ORDER BY grade, surname, name")
    students = [dict(row) for row in c.fetchall()]
    return templates.TemplateResponse(
        request=request,
        name="teacher_homework_detail.html",
        context={
            "user": user,
            "homework": homework,
            "tests": tests,
            "students": students,
            "submissions": submissions,
            "sub_map": sub_map,
        },
    )


@app.get("/teacher/homework/{homework_id}/edit", response_class=HTMLResponse)
def teacher_homework_edit_page(
    homework_id: int,
    request: Request,
    user: dict = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    if not user or user["role"] != "teacher":
        return RedirectResponse("/login")
    homework = _get_homework(db, homework_id)
    if not homework or homework["created_by"] != user["id"]:
        raise HTTPException(status_code=404)
    c = db.cursor()
    c.execute("SELECT * FROM homework_tests WHERE homework_id = ? ORDER BY id", (homework_id,))
    tests = [dict(row) for row in c.fetchall()]
    return templates.TemplateResponse(
        request=request,
        name="teacher_homework_edit.html",
        context={"user": user, "homework": homework, "tests": tests},
    )


@app.post("/teacher/homework/{homework_id}/edit")
async def teacher_homework_update(
    homework_id: int,
    request: Request,
    user: dict = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    if not user or user["role"] != "teacher":
        raise HTTPException(status_code=403)
    homework = _get_homework(db, homework_id)
    if not homework or homework["created_by"] != user["id"]:
        raise HTTPException(status_code=404)
    form = await request.form()
    data, error = _parse_homework_form(form)
    if error:
        return RedirectResponse(
            f"/teacher/homework/{homework_id}/edit?error={error}",
            status_code=status.HTTP_302_FOUND,
        )

    c = db.cursor()
    c.execute(
        """UPDATE homework
           SET title = ?, description = ?, starter_code = ?, max_score = ?, due_date = ?, grade = ?,
               check_mode = ?, check_type = ?, fixed_answer = ?
           WHERE id = ?""",
        (
            data["title"],
            data["description"],
            data["starter_code"],
            data["max_score"],
            data["due_date"],
            data["grade"],
            data["check_mode"],
            data["check_type"],
            data["fixed_answer"],
            homework_id,
        ),
    )
    if data["check_type"] == "tests":
        _save_homework_tests(c, homework_id, form)
    else:
        c.execute("DELETE FROM homework_tests WHERE homework_id = ?", (homework_id,))
    db.commit()
    return RedirectResponse(
        f"/teacher/homework/{homework_id}?updated=1",
        status_code=status.HTTP_302_FOUND,
    )


@app.get("/teacher/homework/{homework_id}/submission/{submission_id}", response_class=HTMLResponse)
def teacher_homework_submission(
    homework_id: int,
    submission_id: int,
    request: Request,
    user: dict = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    if not user or user["role"] != "teacher":
        return RedirectResponse("/login")
    homework = _get_homework(db, homework_id)
    if not homework or homework["created_by"] != user["id"]:
        raise HTTPException(status_code=404)
    c = db.cursor()
    c.execute(
        """
        SELECT hs.*, u.name, u.surname, u.username, u.grade, u.photo_path
        FROM homework_submissions hs
        JOIN users u ON u.id = hs.student_id
        WHERE hs.id = ? AND hs.homework_id = ?
        """,
        (submission_id, homework_id),
    )
    submission = c.fetchone()
    if not submission:
        raise HTTPException(status_code=404)
    try:
        test_results = json.loads(submission["test_results_json"] or "{}")
    except Exception:
        test_results = {}
    return templates.TemplateResponse(
        request=request,
        name="homework_result.html",
        context={
            "user": user,
            "homework": homework,
            "submission": submission,
            "test_results": test_results,
            "is_teacher": True,
        },
    )


@app.post("/teacher/homework/{homework_id}/submission/{submission_id}/grade")
def teacher_homework_manual_grade(
    homework_id: int,
    submission_id: int,
    score: int = Form(...),
    explanation: str = Form(""),
    user: dict = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    if not user or user["role"] != "teacher":
        raise HTTPException(status_code=403)
    homework = _get_homework(db, homework_id)
    if not homework or homework["created_by"] != user["id"]:
        raise HTTPException(status_code=404)
    max_score = int(homework["max_score"] or DEFAULT_MAX_SCORE)
    final_score = clamp_score(score, max_score)
    c = db.cursor()
    c.execute(
        """UPDATE homework_submissions
           SET score = ?, max_score = ?, status = 'done', explanation = ?, checked_at = ?
           WHERE id = ? AND homework_id = ?""",
        (final_score, max_score, explanation.strip(), int(time.time()), submission_id, homework_id),
    )
    db.commit()
    return RedirectResponse(
        f"/teacher/homework/{homework_id}/submission/{submission_id}",
        status_code=status.HTTP_302_FOUND,
    )


# --- TEACHER ATTENDANCE / DISCIPLINE / SCORING ---

@app.get("/teacher/attendance", response_class=HTMLResponse)
def teacher_attendance_page(
    request: Request,
    user: dict = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    if not user or user["role"] != "teacher":
        return RedirectResponse("/login")
    grades = _grades_with_students(db)
    grade = _resolve_grade(db, request.query_params.get("grade"))
    selected_date = _normalize_date(request.query_params.get("date")) or _today_str()
    students = _teacher_students(db, grade) if grade else []
    config = _get_attendance_config(db, grade) if grade else dict(DEFAULT_ATTENDANCE_CONFIG)

    records = {}
    bonuses = {}
    if grade:
        c = db.cursor()
        c.execute(
            "SELECT * FROM attendance_records WHERE grade = ? AND date = ?",
            (grade, selected_date),
        )
        records = {row["student_id"]: dict(row) for row in c.fetchall()}
        c.execute(
            "SELECT * FROM discipline_records WHERE grade = ? AND date = ? AND source = 'attendance'",
            (grade, selected_date),
        )
        bonuses = {row["student_id"]: row["score_delta"] for row in c.fetchall()}

    return templates.TemplateResponse(
        request=request,
        name="teacher_attendance.html",
        context={
            "user": user,
            "grades": grades,
            "grade": grade,
            "selected_date": selected_date,
            "students": students,
            "config": config,
            "records": records,
            "bonuses": bonuses,
        },
    )


@app.post("/teacher/attendance")
async def teacher_attendance_save(
    request: Request,
    user: dict = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    if not user or user["role"] != "teacher":
        raise HTTPException(status_code=403)
    form = await request.form()
    grades = _grades_with_students(db)
    grade = _resolve_grade(db, str(form.get("grade") or ""))
    selected_date = _normalize_date(form.get("date"))
    if not grade or not selected_date or grade not in grades:
        return RedirectResponse("/teacher/attendance?error=invalid", status_code=status.HTTP_302_FOUND)

    config = _get_attendance_config(db, grade)
    score_map = {
        "present": int(config.get("present_score") or 0),
        "late": int(config.get("late_score") or 0),
        "absent": int(config.get("absent_score") or 0),
    }
    students = _teacher_students(db, grade)
    now = int(time.time())
    c = db.cursor()
    for student in students:
        status_value = str(form.get(f"status_{student['id']}") or "").strip().lower()
        if status_value not in score_map:
            continue
        c.execute(
            """INSERT INTO attendance_records
               (student_id, grade, date, status, score, created_by, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(student_id, date) DO UPDATE SET
                 grade = excluded.grade,
                 status = excluded.status,
                 score = excluded.score,
                 created_by = excluded.created_by,
                 created_at = excluded.created_at""",
            (student["id"], grade, selected_date, status_value, score_map[status_value], user["id"], now),
        )

        c.execute(
            "DELETE FROM discipline_records WHERE student_id = ? AND date = ? AND source = 'attendance'",
            (student["id"], selected_date),
        )
        bonus = _clamp_delta(form.get(f"bonus_{student['id']}"), -100, 100)
        if bonus:
            c.execute(
                """INSERT INTO discipline_records
                   (student_id, grade, date, note, score_delta, source, created_by, created_at)
                   VALUES (?, ?, ?, ?, ?, 'attendance', ?, ?)""",
                (student["id"], grade, selected_date, "Хичээлд идэвхтэй оролцсон", bonus, user["id"], now),
            )
    db.commit()
    return RedirectResponse(
        f"/teacher/attendance?grade={grade}&date={selected_date}&saved=1",
        status_code=status.HTTP_302_FOUND,
    )


@app.get("/teacher/attendance-settings", response_class=HTMLResponse)
def teacher_attendance_settings_page(
    request: Request,
    user: dict = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    if not user or user["role"] != "teacher":
        return RedirectResponse("/login")
    grades = _grades_with_students(db)
    grade = _resolve_grade(db, request.query_params.get("grade"))
    config = _get_attendance_config(db, grade) if grade else dict(DEFAULT_ATTENDANCE_CONFIG)
    return templates.TemplateResponse(
        request=request,
        name="teacher_attendance_settings.html",
        context={"user": user, "grades": grades, "grade": grade, "config": config},
    )


@app.post("/teacher/attendance-settings")
async def teacher_attendance_settings_save(
    request: Request,
    user: dict = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    if not user or user["role"] != "teacher":
        raise HTTPException(status_code=403)
    form = await request.form()
    grade = _resolve_grade(db, str(form.get("grade") or ""))
    if not grade:
        return RedirectResponse("/teacher/attendance-settings?error=grade", status_code=status.HTTP_302_FOUND)
    present_score = _clamp_delta(form.get("present_score"), -100, 100)
    late_score = _clamp_delta(form.get("late_score"), -100, 100)
    absent_score = _clamp_delta(form.get("absent_score"), -100, 100)
    c = db.cursor()
    c.execute(
        """INSERT INTO attendance_config (grade, present_score, late_score, absent_score, updated_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(grade) DO UPDATE SET
             present_score = excluded.present_score,
             late_score = excluded.late_score,
             absent_score = excluded.absent_score,
             updated_at = excluded.updated_at""",
        (grade, present_score, late_score, absent_score, int(time.time())),
    )
    db.commit()
    return RedirectResponse(
        f"/teacher/attendance-settings?grade={grade}&saved=1",
        status_code=status.HTTP_302_FOUND,
    )


@app.get("/teacher/discipline", response_class=HTMLResponse)
def teacher_discipline_page(
    request: Request,
    user: dict = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    if not user or user["role"] != "teacher":
        return RedirectResponse("/login")
    grades = _grades_with_students(db)
    grade = _resolve_grade(db, request.query_params.get("grade"))
    selected_date = _normalize_date(request.query_params.get("date")) or _today_str()
    students = _teacher_students(db, grade) if grade else []
    c = db.cursor()
    c.execute(
        """
        SELECT dr.*, u.name, u.surname, u.username
        FROM discipline_records dr
        JOIN users u ON u.id = dr.student_id
        WHERE dr.grade = ?
        ORDER BY dr.date DESC, dr.id DESC
        LIMIT 60
        """,
        (grade,),
    )
    recent = [dict(row) for row in c.fetchall()]
    return templates.TemplateResponse(
        request=request,
        name="teacher_discipline.html",
        context={
            "user": user,
            "grades": grades,
            "grade": grade,
            "selected_date": selected_date,
            "students": students,
            "recent": recent,
        },
    )


@app.post("/teacher/discipline")
async def teacher_discipline_save(
    request: Request,
    user: dict = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    if not user or user["role"] != "teacher":
        raise HTTPException(status_code=403)
    form = await request.form()
    grade = _resolve_grade(db, str(form.get("grade") or ""))
    selected_date = _normalize_date(form.get("date"))
    try:
        student_id = int(str(form.get("student_id") or "").strip())
    except ValueError:
        student_id = 0
    delta = _clamp_delta(form.get("score_delta"), -100, 100)
    note = str(form.get("note") or "").strip()
    if not grade or not selected_date or not student_id or delta == 0:
        return RedirectResponse(
            f"/teacher/discipline?grade={grade}&date={selected_date or _today_str()}&error=invalid",
            status_code=status.HTTP_302_FOUND,
        )
    c = db.cursor()
    c.execute(
        "SELECT id FROM users WHERE id = ? AND role = 'student' AND grade = ?",
        (student_id, grade),
    )
    if not c.fetchone():
        return RedirectResponse(
            f"/teacher/discipline?grade={grade}&date={selected_date}&error=student",
            status_code=status.HTTP_302_FOUND,
        )
    if not note:
        note = "Хичээлд идэвхтэй оролцсон" if delta > 0 else "Сахилгын зөрчил"
    c.execute(
        """INSERT INTO discipline_records
           (student_id, grade, date, note, score_delta, source, created_by, created_at)
           VALUES (?, ?, ?, ?, ?, 'manual', ?, ?)""",
        (student_id, grade, selected_date, note, delta, user["id"], int(time.time())),
    )
    db.commit()
    return RedirectResponse(
        f"/teacher/discipline?grade={grade}&date={selected_date}&saved=1",
        status_code=status.HTTP_302_FOUND,
    )


@app.post("/teacher/discipline/{record_id}/delete")
def teacher_discipline_delete(
    record_id: int,
    grade: str = Form(""),
    date: str = Form(""),
    user: dict = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    if not user or user["role"] != "teacher":
        raise HTTPException(status_code=403)
    c = db.cursor()
    c.execute("DELETE FROM discipline_records WHERE id = ?", (record_id,))
    db.commit()
    grade = _resolve_grade(db, grade)
    selected_date = _normalize_date(date) or _today_str()
    return RedirectResponse(
        f"/teacher/discipline?grade={grade}&date={selected_date}",
        status_code=status.HTTP_302_FOUND,
    )


@app.get("/teacher/scoring", response_class=HTMLResponse)
def teacher_scoring_page(
    request: Request,
    user: dict = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    if not user or user["role"] != "teacher":
        return RedirectResponse("/login")
    grades = _grades_with_students(db)
    grade = _resolve_grade(db, request.query_params.get("grade"))
    c = db.cursor()
    c.execute(
        """
        SELECT u.id, u.name, u.surname, u.username, u.grade, u.photo_path,
          COALESCE((SELECT SUM(hs.score) FROM homework_submissions hs
                    WHERE hs.student_id = u.id AND hs.status = 'done'), 0) AS homework_total,
          COALESCE((SELECT SUM(ar.score) FROM attendance_records ar
                    WHERE ar.student_id = u.id), 0) AS attendance_total,
          COALESCE((SELECT SUM(dr.score_delta) FROM discipline_records dr
                    WHERE dr.student_id = u.id), 0) AS discipline_total
        FROM users u
        WHERE u.role = 'student' AND u.grade = ?
        ORDER BY u.surname, u.name
        """,
        (grade,),
    )
    rows = []
    for row in c.fetchall():
        item = dict(row)
        item["total"] = int(item["homework_total"]) + int(item["attendance_total"]) + int(item["discipline_total"])
        rows.append(item)
    return templates.TemplateResponse(
        request=request,
        name="teacher_scoring.html",
        context={"user": user, "grades": grades, "grade": grade, "rows": rows},
    )


# --- STUDENT DASHBOARD & ASSESSMENT ---
@app.get("/student", response_class=HTMLResponse)
def student_dashboard(request: Request, user: dict = Depends(get_current_user), db: sqlite3.Connection = Depends(get_db)):
    if not user or user["role"] != "student":
        return RedirectResponse("/login")
    c = db.cursor()
    c.execute("SELECT * FROM lessons")
    lessons = c.fetchall()
    c.execute('''
        SELECT quizzes.*, lessons.title as lesson_title,
        (SELECT id FROM submissions WHERE submissions.quiz_id = quizzes.id AND submissions.student_id = ?) as sub_id
        FROM quizzes
        JOIN lessons ON quizzes.lesson_id = lessons.id
        WHERE COALESCE(quizzes.is_active, 1) = 1
        ORDER BY quizzes.id DESC
    ''', (user["id"],))
    quizzes = c.fetchall()
    c.execute(
        """
        SELECT h.*, hs.id AS sub_id, hs.status AS sub_status, hs.score AS sub_score,
               hs.max_score AS sub_max_score
        FROM homework h
        LEFT JOIN homework_submissions hs
               ON hs.homework_id = h.id AND hs.student_id = ?
        WHERE h.is_active = 1
          AND (h.grade IS NULL OR h.grade = '' OR h.grade = ?)
        ORDER BY (h.due_date IS NULL OR h.due_date = ''), h.due_date ASC, h.id DESC
        """,
        (user["id"], user["grade"] or ""),
    )
    homeworks = [dict(row) for row in c.fetchall()]
    score_summary = _student_score_summary(db, user["id"])
    c.execute(
        "SELECT * FROM attendance_records WHERE student_id = ? ORDER BY date DESC, id DESC LIMIT 5",
        (user["id"],),
    )
    recent_attendance = [dict(row) for row in c.fetchall()]
    c.execute(
        """SELECT * FROM discipline_records WHERE student_id = ?
           ORDER BY date DESC, id DESC LIMIT 5""",
        (user["id"],),
    )
    recent_discipline = [dict(row) for row in c.fetchall()]
    return templates.TemplateResponse(
        request=request,
        name="student.html",
        context={
            "user": user,
            "lessons": lessons,
            "quizzes": quizzes,
            "homeworks": homeworks,
            "score_summary": score_summary,
            "recent_attendance": recent_attendance,
            "recent_discipline": recent_discipline,
        }
    )


@app.get("/student/homework/{homework_id}", response_class=HTMLResponse)
def student_homework_page(
    homework_id: int,
    request: Request,
    user: dict = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    if not user or user["role"] != "student":
        return RedirectResponse("/login")
    homework = _get_homework(db, homework_id)
    if not _student_can_view_homework(homework, user):
        raise HTTPException(status_code=404)
    c = db.cursor()
    c.execute("SELECT COUNT(*) FROM homework_tests WHERE homework_id = ?", (homework_id,))
    test_count = c.fetchone()[0]
    c.execute(
        "SELECT * FROM homework_submissions WHERE homework_id = ? AND student_id = ?",
        (homework_id, user["id"]),
    )
    submission = c.fetchone()
    test_results = {}
    if submission:
        try:
            test_results = json.loads(submission["test_results_json"] or "{}")
        except Exception:
            test_results = {}
    return templates.TemplateResponse(
        request=request,
        name="student_homework.html",
        context={
            "user": user,
            "homework": homework,
            "test_count": test_count,
            "submission": submission,
            "test_results": test_results,
            "is_teacher": False,
        },
    )


@app.post("/student/homework/{homework_id}/submit")
async def student_homework_submit(
    homework_id: int,
    request: Request,
    user: dict = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    if not user or user["role"] != "student":
        raise HTTPException(status_code=403)
    homework = _get_homework(db, homework_id)
    if not _student_can_view_homework(homework, user):
        raise HTTPException(status_code=404)
    form = await request.form()
    if (homework["check_type"] or "tests") == "fixed":
        code = str(form.get("answer") or "")
    else:
        code = str(form.get("code") or "")
    if not code.strip():
        return RedirectResponse(
            f"/student/homework/{homework_id}?error=empty",
            status_code=status.HTTP_302_FOUND,
        )

    max_score = int(homework["max_score"] or DEFAULT_MAX_SCORE)
    now = int(time.time())
    c = db.cursor()
    c.execute(
        "SELECT id FROM homework_submissions WHERE homework_id = ? AND student_id = ?",
        (homework_id, user["id"]),
    )
    existing = c.fetchone()
    if existing:
        submission_id = existing["id"]
        c.execute(
            """UPDATE homework_submissions
               SET code = ?, status = 'checking', score = NULL, max_score = ?, explanation = NULL,
                   test_results_json = NULL, submitted_at = ?, checked_at = NULL
               WHERE id = ?""",
            (code, max_score, now, submission_id),
        )
    else:
        c.execute(
            """INSERT INTO homework_submissions
               (homework_id, student_id, code, status, score, max_score, submitted_at)
               VALUES (?, ?, ?, 'checking', NULL, ?, ?)""",
            (homework_id, user["id"], code, max_score, now),
        )
        submission_id = c.lastrowid
    db.commit()

    if (homework["check_mode"] or "recursive") == "queue":
        threading.Thread(target=_grade_in_background, args=(submission_id,), daemon=True).start()
    else:
        try:
            _grade_and_store(db, submission_id)
        except Exception as exc:
            c.execute(
                "UPDATE homework_submissions SET status = 'error', explanation = ?, checked_at = ? WHERE id = ?",
                (f"Шалгах үед алдаа гарлаа: {exc}", int(time.time()), submission_id),
            )
            db.commit()

    return RedirectResponse(
        f"/student/homework/{homework_id}?submitted=1",
        status_code=status.HTTP_302_FOUND,
    )


@app.get("/lesson/view/{lesson_id}")
def view_lesson(lesson_id: int, db: sqlite3.Connection = Depends(get_db)):
    c = db.cursor()
    c.execute("SELECT * FROM lessons WHERE id = ?", (lesson_id,))
    lesson = c.fetchone()
    if not lesson:
        raise HTTPException(status_code=404, detail="Lesson not found")
    file_path = os.path.join(UPLOAD_LESSONS, lesson["filename"])
    return FileResponse(file_path, media_type="text/html")

@app.get("/quiz/image/{filename}")
def get_quiz_image(filename: str):
    return FileResponse(os.path.join(UPLOAD_IMAGES, filename))


@app.get("/photo/{filename}")
def get_student_photo(filename: str):
    return FileResponse(os.path.join(UPLOAD_PHOTOS, os.path.basename(filename)))

@app.get("/quiz/take/{quiz_id}", response_class=HTMLResponse)
def take_quiz_page(quiz_id: int, request: Request, user: dict = Depends(get_current_user), db: sqlite3.Connection = Depends(get_db)):
    if not user or user["role"] != "student":
        return RedirectResponse("/login")
    c = db.cursor()
    c.execute("SELECT * FROM quizzes WHERE id = ?", (quiz_id,))
    quiz = c.fetchone()
    if not quiz:
        raise HTTPException(status_code=404)
    if not quiz["is_active"]:
        return RedirectResponse("/student?error=exam_inactive", status_code=status.HTTP_302_FOUND)

    c.execute("SELECT id FROM submissions WHERE quiz_id = ? AND student_id = ?", (quiz_id, user["id"]))
    existing_sub = c.fetchone()
    if existing_sub:
        return RedirectResponse(f"/quiz/result/{existing_sub['id']}", status_code=status.HTTP_302_FOUND)

    time_limit = quiz["time_limit_minutes"] if "time_limit_minutes" in quiz.keys() else None
    time_remaining = None
    if time_limit:
        now = int(time.time())
        c.execute("SELECT id, started_at, submitted_at FROM quiz_attempts WHERE quiz_id = ? AND student_id = ?", (quiz_id, user["id"]))
        attempt = c.fetchone()
        if attempt and attempt["submitted_at"]:
            c.execute("SELECT id FROM submissions WHERE quiz_id = ? AND student_id = ?", (quiz_id, user["id"]))
            sub = c.fetchone()
            if sub:
                return RedirectResponse(f"/quiz/result/{sub['id']}", status_code=status.HTTP_302_FOUND)
        if not attempt:
            c.execute("INSERT INTO quiz_attempts (quiz_id, student_id, started_at) VALUES (?, ?, ?)", (quiz_id, user["id"], now))
            db.commit()
            started_at = now
        else:
            started_at = attempt["started_at"]
        deadline = started_at + int(time_limit) * 60
        time_remaining = max(0, deadline - now)
        if time_remaining <= 0:
            quiz_data = json.loads(quiz["questions_json"])
            student_answers, criterion_scores, total_score = score_quiz(quiz_data["questions"], {})
            eval_result = {
                "criteria_scores": criterion_scores,
                "total_score": total_score,
                "overall_summary": f"{total_score} / 8 асуултад зөв хариулсан. (Хугацаа дууссан — автоматаар илгээгдсэн.)",
                "timed_out": True,
            }
            c.execute(
                "INSERT INTO submissions (quiz_id, student_id, responses_json, evaluation_json, total_score) VALUES (?, ?, ?, ?, ?)",
                (quiz_id, user["id"], json.dumps(student_answers), json.dumps(eval_result), total_score)
            )
            sub_id = c.lastrowid
            c.execute("UPDATE quiz_attempts SET submitted_at = ? WHERE quiz_id = ? AND student_id = ?", (now, quiz_id, user["id"]))
            db.commit()
            return RedirectResponse(f"/quiz/result/{sub_id}", status_code=status.HTTP_302_FOUND)

    quiz_data = json.loads(quiz["questions_json"])
    return templates.TemplateResponse(
        request=request,
        name="take_quiz.html",
        context={
            "user": user,
            "quiz": quiz,
            "quiz_data": quiz_data,
            "time_limit_minutes": time_limit,
            "time_remaining": time_remaining,
        }
    )


@app.post("/quiz/submit/{quiz_id}")
async def submit_quiz(
    quiz_id: int,
    request: Request,
    user: dict = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db)
):
    if not user or user["role"] != "student":
        raise HTTPException(status_code=403)
    
    c = db.cursor()
    c.execute("SELECT id FROM submissions WHERE quiz_id = ? AND student_id = ?", (quiz_id, user["id"]))
    existing = c.fetchone()
    if existing:
        return RedirectResponse(f"/quiz/result/{existing['id']}", status_code=status.HTTP_302_FOUND)

    c.execute("SELECT * FROM quizzes WHERE id = ?", (quiz_id,))
    quiz_row = c.fetchone()
    if not quiz_row:
        raise HTTPException(status_code=404, detail="Quiz not found")
    if not quiz_row["is_active"]:
        raise HTTPException(status_code=403, detail="Exam is not active")

    form_data = await request.form()
    timed_out = str(form_data.get("timed_out", "")).strip() in ("1", "true", "yes")
    time_limit = quiz_row["time_limit_minutes"] if "time_limit_minutes" in quiz_row.keys() else None
    if time_limit:
        c.execute("SELECT started_at FROM quiz_attempts WHERE quiz_id = ? AND student_id = ?", (quiz_id, user["id"]))
        att = c.fetchone()
        if att:
            deadline = att["started_at"] + int(time_limit) * 60
            if int(time.time()) > deadline + 5:
                timed_out = True

    quiz_data = json.loads(quiz_row["questions_json"])
    student_answers, criterion_scores, total_score = score_quiz(
        quiz_data["questions"], form_data
    )
    summary = f"{total_score} / 8 асуултад зөв хариулсан."
    if timed_out:
        summary += " (Хугацаа дууссан — автоматаар илгээгдсэн.)"
    eval_result = {
        "criteria_scores": criterion_scores,
        "total_score": total_score,
        "overall_summary": summary,
        "timed_out": timed_out,
    }

    c.execute(
        "INSERT INTO submissions (quiz_id, student_id, responses_json, evaluation_json, total_score) VALUES (?, ?, ?, ?, ?)",
        (quiz_id, user["id"], json.dumps(student_answers), json.dumps(eval_result), total_score)
    )
    sub_id = c.lastrowid
    c.execute("UPDATE quiz_attempts SET submitted_at = ? WHERE quiz_id = ? AND student_id = ?", (int(time.time()), quiz_id, user["id"]))
    db.commit()
    return RedirectResponse(f"/quiz/result/{sub_id}", status_code=status.HTTP_302_FOUND)

@app.get("/quiz/result/{submission_id}", response_class=HTMLResponse)
def view_result(
    submission_id: int,
    request: Request,
    user: dict = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db)
):
    if not user:
        return RedirectResponse("/login")
    c = db.cursor()
    c.execute('''
        SELECT s.*, q.title as quiz_title 
        FROM submissions s 
        JOIN quizzes q ON s.quiz_id = q.id 
        WHERE s.id = ?
    ''', (submission_id,))
    sub = c.fetchone()
    if not sub:
        raise HTTPException(status_code=404)
    
    eval_data = json.loads(sub["evaluation_json"])
    answers_data = json.loads(sub["responses_json"])
    
    # return templates.TemplateResponse( request=request, name="result.html")
    return templates.TemplateResponse(
        request=request, 
        name="result.html", 
        context={
            "user": user,
            "sub": sub,
            "eval": eval_data,
            "answers": answers_data
        }
    )
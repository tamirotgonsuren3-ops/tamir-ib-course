import os
import json
import re
import sqlite3
import shutil
import uuid
from typing import Optional
from fastapi import FastAPI, Request, Form, File, UploadFile, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from quiz_engine import generate_quiz, score_quiz

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


templates.env.globals["ib_to_100"] = ib_to_100
templates.env.globals["grade_options"] = GRADE_OPTIONS

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
    _ensure_columns(c, "quizzes", {"is_active": "is_active INTEGER DEFAULT 1"})
    c.execute("UPDATE quizzes SET is_active = 1 WHERE is_active IS NULL")
    # Сурагчийн хариултууд ба IB үнэлгээ
    c.execute('''CREATE TABLE IF NOT EXISTS submissions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        quiz_id INTEGER,
        student_id INTEGER,
        responses_json TEXT,
        evaluation_json TEXT,
        total_score INTEGER
    )''')
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

    c = db.cursor()
    c.execute(
        "INSERT INTO quizzes (lesson_id, title, questions_json, image_path, created_by, is_active) VALUES (?, ?, ?, ?, ?, 1)",
        (lesson_id, quiz_title, json.dumps(parsed_quiz.model_dump()), image_saved_path, user["id"])
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
    return templates.TemplateResponse(
        request=request,
        name="student.html",
        context={
            "user": user,
            "lessons": lessons,
            "quizzes": quizzes
        }
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
    quiz_data = json.loads(quiz["questions_json"])
    return templates.TemplateResponse(
        request=request,
        name="take_quiz.html",
        context={
            "user": user,
            "quiz": quiz,
            "quiz_data": quiz_data
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
    c.execute("SELECT * FROM quizzes WHERE id = ?", (quiz_id,))
    quiz_row = c.fetchone()
    if not quiz_row:
        raise HTTPException(status_code=404, detail="Quiz not found")
    if not quiz_row["is_active"]:
        raise HTTPException(status_code=403, detail="Exam is not active")

    form_data = await request.form()
    quiz_data = json.loads(quiz_row["questions_json"])
    student_answers, criterion_scores, total_score = score_quiz(
        quiz_data["questions"], form_data
    )
    eval_result = {
        "criteria_scores": criterion_scores,
        "total_score": total_score,
        "overall_summary": f"{total_score} / 8 асуултад зөв хариулсан.",
    }

    # Save submission
    c.execute(
        "INSERT INTO submissions (quiz_id, student_id, responses_json, evaluation_json, total_score) VALUES (?, ?, ?, ?, ?)",
        (quiz_id, user["id"], json.dumps(student_answers), json.dumps(eval_result), total_score)
    )
    db.commit()
    sub_id = c.lastrowid
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
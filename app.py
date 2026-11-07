import os
import json
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
os.makedirs(UPLOAD_LESSONS, exist_ok=True)
os.makedirs(UPLOAD_IMAGES, exist_ok=True)

templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# --- DATABASE SETUP (SQLite) ---
DB_PATH = os.path.join(BASE_DIR, "edupath.db")

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
    user_columns = {row[1] for row in c.execute("PRAGMA table_info(users)").fetchall()}
    if "deepseek_api_key" not in user_columns:
        c.execute("ALTER TABLE users ADD COLUMN deepseek_api_key TEXT")
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
    return templates.TemplateResponse( request=request, name="login.html")

@app.post("/register")
def register(
    username: str = Form(...),
    password: str = Form(...),
    role: str = Form(...),
    db: sqlite3.Connection = Depends(get_db)
):
    c = db.cursor()
    try:
        c.execute("INSERT INTO users (username, password, role) VALUES (?, ?, ?)", (username, password, role))
        db.commit()
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=400, detail="Username already exists")
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
    c.execute("SELECT quizzes.*, lessons.title as lesson_title FROM quizzes JOIN lessons ON quizzes.lesson_id = lessons.id ORDER BY quizzes.id DESC")
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
        "INSERT INTO quizzes (lesson_id, title, questions_json, image_path, created_by) VALUES (?, ?, ?, ?, ?)",
        (lesson_id, quiz_title, json.dumps(parsed_quiz.model_dump()), image_saved_path, user["id"])
    )
    db.commit()
    return RedirectResponse("/teacher?quiz_created=1", status_code=status.HTTP_302_FOUND)

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
    ''', (user["id"],))
    quizzes = c.fetchall()
    # return templates.TemplateResponse(request=request, name="student.html")
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

@app.get("/quiz/take/{quiz_id}", response_class=HTMLResponse)
def take_quiz_page(quiz_id: int, request: Request, user: dict = Depends(get_current_user), db: sqlite3.Connection = Depends(get_db)):
    if not user or user["role"] != "student":
        return RedirectResponse("/login")
    c = db.cursor()
    c.execute("SELECT * FROM quizzes WHERE id = ?", (quiz_id,))
    quiz = c.fetchone()
    if not quiz:
        raise HTTPException(status_code=404)
    quiz_data = json.loads(quiz["questions_json"])
    # return templates.TemplateResponse(request=request, name="take_quiz.html")
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
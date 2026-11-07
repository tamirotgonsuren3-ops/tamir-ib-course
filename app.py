import os
import json
import sqlite3
import shutil
from typing import List, Optional
from fastapi import FastAPI, Request, Form, File, UploadFile, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from PIL import Image

# Google GenAI New Official SDK
from google import genai
from google.genai import types

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
        gemini_api_key TEXT
    )''')
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
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()

# --- PYDANTIC SCHEMAS FOR STRUCTURED GEMINI OUTPUT ---
class QuizQuestion(BaseModel):
    id: int
    question_type: str = Field(description="'multiple_choice' or 'written_response'")
    target_criteria: str = Field(description="IB Criteria: 'Criterion A', 'Criterion B', 'Criterion C', or 'Criterion D'")
    question_text: str
    options: Optional[List[str]] = Field(default=None, description="4 choices for multiple choice")
    correct_option_index: Optional[int] = Field(default=None, description="Index 0-3 if multiple choice")
    rubric_guidance: str = Field(description="What an excellent answer must include for IB evaluation")

class GeneratedQuiz(BaseModel):
    quiz_title: str
    questions: List[QuizQuestion]

class CriterionScore(BaseModel):
    criterion: str = Field(description="Criterion A, B, C, or D")
    score: int = Field(description="IB score from 0 to 8")
    descriptor: str = Field(description="e.g. 1-2 Limited, 3-4 Adequate, 5-6 Substantial, 7-8 Excellent")
    strengths: str
    areas_for_improvement: str

class IBAssessmentResult(BaseModel):
    overall_summary: str
    criteria_scores: List[CriterionScore]
    final_grade_boundary: str = Field(description="IB Grade from 1 to 7 based on aggregate performance")
    teacher_notes: str

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
    c.execute("SELECT quizzes.*, lessons.title as lesson_title FROM quizzes JOIN lessons ON quizzes.lesson_id = lessons.id WHERE quizzes.created_by = ?", (user["id"],))
    quizzes = c.fetchall()
    # return templates.TemplateResponse( request=request, name="teacher.html")
    return templates.TemplateResponse(
        request=request, 
        name="teacher.html", 
        context={
            "user": user,
            "lessons": lessons,
            "quizzes": quizzes
        }
    )


@app.post("/teacher/api-key")
def update_api_key(
    api_key: str = Form(...),
    user: dict = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db)
):
    if not user or user["role"] != "teacher":
        raise HTTPException(status_code=403)
    c = db.cursor()
    c.execute("UPDATE users SET gemini_api_key = ? WHERE id = ?", (api_key.strip(), user["id"]))
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
    
    # Save lesson.html
    safe_filename = f"{user['id']}_{file.filename}"
    file_path = os.path.join(UPLOAD_LESSONS, safe_filename)
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
    
    c = db.cursor()
    c.execute("INSERT INTO lessons (title, filename, teacher_id) VALUES (?, ?, ?)", (title, safe_filename, user["id"]))
    db.commit()
    return RedirectResponse("/teacher?lesson_uploaded=1", status_code=status.HTTP_302_FOUND)

@app.post("/teacher/create-quiz")
async def create_quiz_with_gemini(
    lesson_id: int = Form(...),
    quiz_title: str = Form(...),
    prompt: str = Form(...),
    image: Optional[UploadFile] = File(None),
    user: dict = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db)
):
    if not user or user["role"] != "teacher":
        raise HTTPException(status_code=403)
    if not user["gemini_api_key"]:
        return RedirectResponse("/teacher?error=no_api_key", status_code=status.HTTP_302_FOUND)

    # Initialize Google GenAI Client
    client = genai.Client(api_key=user["gemini_api_key"])
    contents = []

    # Handle Image if uploaded
    image_saved_path = None
    if image and image.filename:
        safe_img_name = f"quiz_{user['id']}_{image.filename}"
        img_dest = os.path.join(UPLOAD_IMAGES, safe_img_name)
        with open(img_dest, "wb") as buf:
            shutil.copyfileobj(image.file, buf)
        image_saved_path = safe_img_name
        
        # Load image with PIL for Gemini SDK
        pil_image = Image.open(img_dest)
        contents.append(pil_image)

    system_instruction = (
        "You are an expert International Baccalaureate (IB) evaluator and business teacher. "
        "Create high-school level quiz questions that test students on business, ERP systems, "
        "accounting, and economics. Ensure you include both multiple-choice and written open-response questions. "
        "Explicitly map questions to IB MYP/DP Assessment Criteria: "
        "Criterion A (Knowing and Understanding), Criterion B (Inquiring/Planning), "
        "Criterion C (Processing/Evaluating), or Criterion D (Reflecting & Real-World Application)."
    )

    full_prompt = f"{system_instruction}\n\nTeacher Request: {prompt}\nTitle: {quiz_title}"
    contents.append(full_prompt)

    try:
        # Generate Structured Quiz using Gemini 2.5 Flash
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=contents,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=GeneratedQuiz,
                temperature=0.3
            ),
        )
        parsed_quiz: GeneratedQuiz = json.loads(response.text)
    except Exception as e:
        print("Gemini Generation Error:", e)
        return RedirectResponse(f"/teacher?error=generation_failed&msg={str(e)}", status_code=status.HTTP_302_FOUND)

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
    c.execute("SELECT q.*, u.gemini_api_key FROM quizzes q JOIN users u ON q.created_by = u.id WHERE q.id = ?", (quiz_id,))
    quiz_row = c.fetchone()
    if not quiz_row or not quiz_row["gemini_api_key"]:
        raise HTTPException(status_code=400, detail="Teacher API Key is missing for grading")

    form_data = await request.form()
    quiz_data = json.loads(quiz_row["questions_json"])
    
    student_answers = []
    for q in quiz_data["questions"]:
        qid = str(q["id"])
        ans = form_data.get(f"q_{qid}", "").strip()
        student_answers.append({
            "id": q["id"],
            "target_criteria": q["target_criteria"],
            "question_text": q["question_text"],
            "question_type": q["question_type"],
            "student_answer": ans,
            "correct_option_index": q.get("correct_option_index"),
            "rubric_guidance": q["rubric_guidance"]
        })

    # Prepare IB Evaluation Prompt for Gemini
    eval_prompt = f"""
    You are an official International Baccalaureate (IB) examiner.
    Evaluate the following student's quiz submission for: "{quiz_row['title']}".
    
    The quiz includes multiple choice and in-depth written questions.
    Grade the student strictly according to IB MYP/DP criteria on a 0 to 8 scale for each relevant criterion:
    - Criterion A: Knowing and understanding (0-8)
    - Criterion B: Inquiring and designing / Planning (0-8)
    - Criterion C: Processing and evaluating / Critical thinking (0-8)
    - Criterion D: Reflecting on the impacts / Real-world application (0-8)
    
    Questions, student responses, and teacher rubric benchmarks:
    {json.dumps(student_answers, indent=2, ensure_ascii=False)}
    
    Provide constructive, inspiring, and professional feedback with specific strengths and areas for improvement.
    """

    client = genai.Client(api_key=quiz_row["gemini_api_key"])
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=eval_prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=IBAssessmentResult,
            temperature=0.2
        ),
    )
    eval_result: IBAssessmentResult = json.loads(response.text)

    # Save submission
    c.execute(
        "INSERT INTO submissions (quiz_id, student_id, responses_json, evaluation_json, total_score) VALUES (?, ?, ?, ?, ?)",
        (quiz_id, user["id"], json.dumps(student_answers), response.text, 0)
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
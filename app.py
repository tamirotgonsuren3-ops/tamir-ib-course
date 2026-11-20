import calendar
import csv
import io
import math
import os
import console_utils  # noqa: F401 - configures UTF-8 console output on Windows
import json
import re
import sqlite3
import shutil
import threading
import time
import uuid
from datetime import datetime, date, timedelta
from typing import Optional
from fastapi import FastAPI, Request, Form, File, UploadFile, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from quiz_engine import generate_quiz, score_quiz
from topic_quizzes import TOPIC_QUIZZES, build_topic_quiz
from data_analysis_engine import grade_document, extract_docx
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
UPLOAD_CSV = os.path.join(BASE_DIR, "uploads", "data_analysis", "csv")
UPLOAD_DOCX = os.path.join(BASE_DIR, "uploads", "data_analysis", "documents")
for _upload_dir in (UPLOAD_LESSONS, UPLOAD_IMAGES, UPLOAD_PHOTOS, UPLOAD_CSV, UPLOAD_DOCX):
    os.makedirs(_upload_dir, exist_ok=True)

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

WEEKDAY_NAMES = ("Даваа", "Мягмар", "Лхагва", "Пүрэв", "Баасан", "Бямба", "Ням")
CRITERION_LETTERS = ("A", "B", "C", "D")
TERM_WEIGHTS = {
    "criteria": 0.4,
    "attendance": 0.2,
    "data_analysis": 0.2,
    "homework": 0.1,
    "exams": 0.1,
}


def _round1(value) -> float:
    try:
        return round(float(value), 1)
    except (TypeError, ValueError):
        return 0.0


def _parse_int(value, default: int = 0, minimum: int = 0, maximum: int = 100000) -> int:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


def _safe_upload_name(original, prefix: str, allowed: set[str]) -> str | None:
    name = os.path.basename((original or "").replace("\\", "/"))
    ext = os.path.splitext(name)[1].lower()
    if ext not in allowed:
        return None
    return f"{prefix}_{uuid.uuid4().hex}{ext}"


def _is_upload(value) -> bool:
    return hasattr(value, "filename") and hasattr(value, "file")


def _save_evidence_photo(value) -> str | None:
    if not _is_upload(value) or not getattr(value, "filename", None):
        return None
    safe_name = _safe_upload_name(
        value.filename, "evidence", {".png", ".jpg", ".jpeg", ".webp", ".gif"}
    )
    if not safe_name:
        return None
    path = os.path.join(UPLOAD_PHOTOS, safe_name)
    with open(path, "wb") as buf:
        shutil.copyfileobj(value.file, buf)
    return safe_name


def _photo_url(photo_path) -> str:
    name = os.path.basename(str(photo_path or ""))
    return f"/photo/{name}" if name else ""


def _photo_file(photo_path) -> str | None:
    name = os.path.basename(str(photo_path or ""))
    if not name:
        return None
    path = os.path.join(UPLOAD_PHOTOS, name)
    return path if os.path.isfile(path) else None


def _parse_date(value: str | None):
    try:
        return datetime.strptime((value or "").strip()[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _weeks_between(start_date: str, end_date: str) -> int:
    start, end = _parse_date(start_date), _parse_date(end_date)
    if not start or not end or end < start:
        return 0
    return max(1, math.ceil(((end - start).days + 1) / 7))


def _default_school_year() -> tuple[str, int]:
    today = date.today()
    start_year = today.year if today.month >= 9 else today.year - 1
    return f"{start_year}-{start_year + 1}", start_year


# --- CSV IMPORT / EXPORT HELPERS ---
def _csv_response(filename: str, rows: list[list]) -> Response:
    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    return Response(
        content="\ufeff" + buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _csv_dicts(text: str) -> list[dict]:
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    return [
        {(k or "").strip(): (v or "").strip() for k, v in row.items() if k}
        for row in reader
    ]


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
        "google_form_url": "google_form_url TEXT",
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
    c.execute('''CREATE TABLE IF NOT EXISTS google_form_results (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        homework_id INTEGER NOT NULL,
        student_id INTEGER NOT NULL,
        student_email TEXT,
        score INTEGER,
        max_score INTEGER,
        source_row_json TEXT,
        imported_at INTEGER
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
    _ensure_columns(c, "discipline_records", {"photo_path": "photo_path TEXT"})
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_discipline_student_date ON discipline_records(student_id, date)"
    )

    # --- Анги, улирал, хичээл, IB Unit Plan, хуваарь, улирлын үнэлгээ ---
    c.execute('''CREATE TABLE IF NOT EXISTS grades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        level INTEGER NOT NULL,
        section TEXT NOT NULL,
        is_active INTEGER NOT NULL DEFAULT 1,
        created_at INTEGER
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS academic_terms (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        school_year TEXT NOT NULL,
        start_date TEXT NOT NULL,
        end_date TEXT NOT NULL,
        weeks INTEGER NOT NULL DEFAULT 6,
        is_active INTEGER NOT NULL DEFAULT 1,
        created_at INTEGER
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS ib_unit_plans (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        lesson_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        unit_code TEXT,
        description TEXT,
        atl_skills TEXT,
        max_score INTEGER NOT NULL DEFAULT 8,
        created_by INTEGER,
        created_at INTEGER
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS lesson_teachers (
        lesson_id INTEGER NOT NULL,
        teacher_id INTEGER NOT NULL,
        PRIMARY KEY (lesson_id, teacher_id)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS lesson_grades (
        lesson_id INTEGER NOT NULL,
        grade_id INTEGER NOT NULL,
        PRIMARY KEY (lesson_id, grade_id)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS lesson_timetables (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        term_id INTEGER NOT NULL,
        lesson_id INTEGER NOT NULL,
        grade_id INTEGER NOT NULL,
        teacher_id INTEGER,
        weekday INTEGER NOT NULL,
        period INTEGER NOT NULL,
        room TEXT,
        created_at INTEGER,
        UNIQUE(term_id, grade_id, weekday, period)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS term_attendance_config (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        term_id INTEGER NOT NULL,
        grade_id INTEGER NOT NULL,
        lesson_id INTEGER NOT NULL DEFAULT 0,
        total_points INTEGER NOT NULL DEFAULT 30,
        atl_skill TEXT NOT NULL DEFAULT 'self-management',
        updated_at INTEGER,
        UNIQUE(term_id, grade_id, lesson_id)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS lesson_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        term_id INTEGER NOT NULL,
        lesson_id INTEGER NOT NULL,
        grade_id INTEGER NOT NULL,
        date TEXT NOT NULL,
        topic TEXT,
        created_by INTEGER,
        created_at INTEGER,
        UNIQUE(term_id, lesson_id, grade_id, date)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS attendance_marks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER NOT NULL,
        student_id INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'present',
        points REAL NOT NULL DEFAULT 0,
        note TEXT,
        UNIQUE(session_id, student_id)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS criteria_scores (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER NOT NULL,
        student_id INTEGER NOT NULL,
        unit_plan_id INTEGER,
        criterion TEXT NOT NULL,
        score REAL NOT NULL DEFAULT 0,
        note TEXT,
        updated_by INTEGER,
        updated_at INTEGER,
        UNIQUE(session_id, student_id, criterion)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS lesson_conduct_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER NOT NULL,
        student_id INTEGER NOT NULL,
        points REAL NOT NULL DEFAULT 0,
        note TEXT,
        created_by INTEGER,
        created_at INTEGER
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS data_analysis_assignments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        term_id INTEGER NOT NULL,
        lesson_id INTEGER NOT NULL,
        grade_id INTEGER NOT NULL,
        unit_plan_id INTEGER,
        title TEXT NOT NULL,
        description TEXT,
        max_score INTEGER NOT NULL DEFAULT 20,
        csv_path TEXT,
        due_date TEXT,
        provider TEXT NOT NULL DEFAULT 'gemini',
        model TEXT,
        created_by INTEGER,
        created_at INTEGER,
        is_active INTEGER NOT NULL DEFAULT 1
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS data_analysis_submissions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        assignment_id INTEGER NOT NULL,
        student_id INTEGER NOT NULL,
        docx_path TEXT NOT NULL,
        file_name TEXT,
        status TEXT NOT NULL DEFAULT 'pending',
        score REAL,
        ib_score REAL,
        criteria_json TEXT,
        feedback TEXT,
        error TEXT,
        submitted_at INTEGER,
        graded_at INTEGER,
        UNIQUE(assignment_id, student_id)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS term_grade_overrides (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        term_id INTEGER NOT NULL,
        student_id INTEGER NOT NULL,
        lesson_id INTEGER NOT NULL DEFAULT 0,
        final_100 REAL,
        ib_score REAL,
        note TEXT,
        updated_by INTEGER,
        updated_at INTEGER,
        UNIQUE(term_id, student_id, lesson_id)
    )''')
    for index_sql in (
        "CREATE INDEX IF NOT EXISTS idx_lesson_timetables_term ON lesson_timetables(term_id)",
        "CREATE INDEX IF NOT EXISTS idx_lesson_sessions_term ON lesson_sessions(term_id, lesson_id, grade_id)",
        "CREATE INDEX IF NOT EXISTS idx_attendance_marks_session ON attendance_marks(session_id)",
        "CREATE INDEX IF NOT EXISTS idx_criteria_scores_session ON criteria_scores(session_id, student_id)",
        "CREATE INDEX IF NOT EXISTS idx_da_submissions_assignment ON data_analysis_submissions(assignment_id)",
    ):
        c.execute(index_sql)

    now = int(time.time())
    for level in range(1, 13):
        for section in ("a", "b", "c"):
            c.execute(
                "INSERT OR IGNORE INTO grades (name, level, section, is_active, created_at) VALUES (?, ?, ?, 1, ?)",
                (f"{level}{section}", level, section, now),
            )
    c.execute("SELECT COUNT(*) FROM academic_terms")
    if c.fetchone()[0] == 0:
        school_year, start_year = _default_school_year()
        default_terms = (
            ("1-р улирал", f"{start_year}-09-01", f"{start_year}-10-31"),
            ("2-р улирал", f"{start_year}-11-01", f"{start_year}-12-31"),
            ("3-р улирал", f"{start_year+1}-01-10", f"{start_year+1}-03-15"),
            ("4-р улирал", f"{start_year+1}-03-16", f"{start_year+1}-05-31"),
        )
        for name, start, end in default_terms:
            c.execute(
                "INSERT INTO academic_terms (name, school_year, start_date, end_date, weeks, is_active, created_at) VALUES (?, ?, ?, ?, ?, 1, ?)",
                (name, school_year, start, end, _weeks_between(start, end), now),
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


# --- IB TERM GRADING HELPERS ---
def _all_grades(db: sqlite3.Connection) -> list[dict]:
    return [dict(row) for row in db.execute(
        "SELECT * FROM grades ORDER BY level, section"
    ).fetchall()]


def _all_terms(db: sqlite3.Connection) -> list[dict]:
    return [dict(row) for row in db.execute(
        "SELECT * FROM academic_terms ORDER BY start_date, id"
    ).fetchall()]


def _grade_name(db: sqlite3.Connection, grade_id: int) -> str:
    row = db.execute("SELECT name FROM grades WHERE id = ?", (grade_id,)).fetchone()
    return row["name"] if row else ""


def _grade_id_by_name(db: sqlite3.Connection, name: str) -> Optional[int]:
    row = db.execute("SELECT id FROM grades WHERE lower(name) = lower(?)", (name or "",)).fetchone()
    return row["id"] if row else None


def _students_for_grade_name(db: sqlite3.Connection, name: str) -> list[dict]:
    return [dict(row) for row in db.execute(
        "SELECT * FROM users WHERE role = 'student' AND lower(grade) = lower(?) ORDER BY surname, name, id",
        (name or "",),
    ).fetchall()]


def _visible_lessons_for_student(db: sqlite3.Connection, grade_name: str) -> list[dict]:
    return [dict(row) for row in db.execute(
        """SELECT l.* FROM lessons l
           WHERE EXISTS (
               SELECT 1 FROM lesson_grades lg JOIN grades g ON g.id = lg.grade_id
               WHERE lg.lesson_id = l.id AND lower(g.name) = lower(?)
           )
           OR NOT EXISTS (SELECT 1 FROM lesson_grades lg WHERE lg.lesson_id = l.id)
           ORDER BY l.id DESC""",
        (grade_name or "",),
    ).fetchall()]


def _attendance_setting(db: sqlite3.Connection, term_id: int, grade_id: int,
                        lesson_id: int = 0) -> dict:
    row = db.execute(
        """SELECT * FROM term_attendance_config
           WHERE term_id = ? AND grade_id = ? AND lesson_id = ?""",
        (term_id, grade_id, lesson_id),
    ).fetchone()
    if not row and lesson_id:
        row = db.execute(
            """SELECT * FROM term_attendance_config
               WHERE term_id = ? AND grade_id = ? AND lesson_id = 0""",
            (term_id, grade_id),
        ).fetchone()
    term = db.execute("SELECT weeks FROM academic_terms WHERE id = ?", (term_id,)).fetchone()
    weeks = max(1, int(term["weeks"] if term else 1))
    total = max(1, int(row["total_points"] if row else 30))
    return {
        "total_points": total,
        "atl_skill": (row["atl_skill"] if row else "self-management") or "self-management",
        "weeks": weeks,
        "per_day": round(total / weeks, 2),
        "ib_per_day": round(total / weeks / total * 8, 2),
        "configured": bool(row),
    }


def _session_ids(db: sqlite3.Connection, term_id: int, lesson_id: int, grade_id: int) -> list[int]:
    return [row["id"] for row in db.execute(
        "SELECT id FROM lesson_sessions WHERE term_id = ? AND lesson_id = ? AND grade_id = ?",
        (term_id, lesson_id, grade_id),
    ).fetchall()]


def _term_homework_stats(db: sqlite3.Connection, term_id: int, student_id: int) -> dict:
    term_row = db.execute(
        "SELECT start_date, end_date FROM academic_terms WHERE id = ?", (term_id,)
    ).fetchone()
    term = dict(term_row) if term_row else None
    score = maximum = 0.0
    count = 0
    for row in db.execute(
        """SELECT h.due_date, hs.score, hs.max_score, hs.submitted_at
           FROM homework_submissions hs JOIN homework h ON h.id = hs.homework_id
           WHERE hs.student_id = ? AND hs.status = 'done'""",
        (student_id,),
    ).fetchall():
        day = row["due_date"] or _ts_day(row["submitted_at"])
        if not _in_term(day, term):
            continue
        score += float(row["score"] or 0)
        maximum += float(row["max_score"] or 0)
        count += 1
    return {
        "score": _round1(score),
        "max": _round1(maximum),
        "count": count,
        "percent": min(100.0, round(score / max(1.0, maximum) * 100, 1)) if maximum else 0.0,
    }


def _term_exam_stats(db: sqlite3.Connection, term_id: int, student_id: int,
                     lesson_id: int | None = None) -> dict:
    term_row = db.execute(
        "SELECT start_date, end_date FROM academic_terms WHERE id = ?", (term_id,)
    ).fetchone()
    term = dict(term_row) if term_row else None
    scores = []
    for row in db.execute(
        """SELECT s.total_score, q.lesson_id, COALESCE(qa.submitted_at, qa.started_at) AS attempt_at
           FROM submissions s JOIN quizzes q ON q.id = s.quiz_id
           LEFT JOIN quiz_attempts qa ON qa.quiz_id = s.quiz_id AND qa.student_id = s.student_id
           WHERE s.student_id = ?""",
        (student_id,),
    ).fetchall():
        if lesson_id and row["lesson_id"] != lesson_id:
            continue
        day = _ts_day(row["attempt_at"])
        if not _in_term(day, term):
            continue
        scores.append(float(row["total_score"] or 0))
    average = sum(scores) / len(scores) if scores else 0.0
    return {
        "count": len(scores),
        "average": _round1(average),
        "percent": min(100.0, round(average / 8.0 * 100, 1)),
    }


def _term_discipline_points(db: sqlite3.Connection, term_id: int, student_id: int) -> dict:
    term = db.execute(
        "SELECT start_date, end_date FROM academic_terms WHERE id = ?", (term_id,)
    ).fetchone()
    if not term:
        return {"total": 0.0, "activity": 0.0, "penalty": 0.0, "count": 0}
    rows = db.execute(
        """SELECT score_delta FROM discipline_records
           WHERE student_id = ? AND date >= ? AND date <= ?""",
        (student_id, term["start_date"], term["end_date"]),
    ).fetchall()
    values = [float(r["score_delta"] or 0) for r in rows]
    return {
        "total": _round1(sum(values)),
        "activity": _round1(sum(v for v in values if v > 0)),
        "penalty": _round1(sum(v for v in values if v < 0)),
        "count": len(values),
    }


def _term_grade_breakdown(db: sqlite3.Connection, term_id: int, student_id: int,
                          lesson_id: int, grade_id: int) -> dict:
    sessions = _session_ids(db, term_id, lesson_id, grade_id)
    session_count = len(sessions)
    setting = _attendance_setting(db, term_id, grade_id, lesson_id)
    placeholders = ",".join("?" for _ in sessions) or "NULL"

    attendance_points = db.execute(
        f"SELECT COALESCE(SUM(points), 0) FROM attendance_marks WHERE session_id IN ({placeholders}) AND student_id = ?",
        (*sessions, student_id),
    ).fetchone()[0] or 0
    attendance_100 = min(100.0, round(float(attendance_points) / setting["total_points"] * 100, 1))

    criteria_sum = db.execute(
        f"SELECT COALESCE(SUM(score), 0) FROM criteria_scores WHERE session_id IN ({placeholders}) AND student_id = ?",
        (*sessions, student_id),
    ).fetchone()[0] or 0
    criteria_ib = min(8.0, _round1(float(criteria_sum) / max(1, session_count)))
    criteria_100 = round(criteria_ib * 12.5, 1)

    conduct_points = db.execute(
        f"SELECT COALESCE(SUM(points), 0) FROM lesson_conduct_records WHERE session_id IN ({placeholders}) AND student_id = ?",
        (*sessions, student_id),
    ).fetchone()[0] or 0

    da_rows = db.execute(
        """SELECT id, max_score FROM data_analysis_assignments
           WHERE term_id = ? AND lesson_id = ? AND grade_id = ? AND is_active = 1""",
        (term_id, lesson_id, grade_id),
    ).fetchall()
    da_max = sum(int(r["max_score"] or 0) for r in da_rows)
    da_score = 0.0
    if da_rows:
        ids = ",".join("?" for _ in da_rows)
        da_score = db.execute(
            f"""SELECT COALESCE(SUM(score), 0) FROM data_analysis_submissions
                WHERE assignment_id IN ({ids}) AND student_id = ? AND status = 'graded'""",
            (*[r["id"] for r in da_rows], student_id),
        ).fetchone()[0] or 0
    da_100 = min(100.0, round(float(da_score) / max(1, da_max) * 100, 1)) if da_max else 0.0
    da_ib = round(da_100 / 12.5, 1)

    homework = _term_homework_stats(db, term_id, student_id)
    exams = _term_exam_stats(db, term_id, student_id, lesson_id if lesson_id else None)
    discipline = _term_discipline_points(db, term_id, student_id)
    discipline_points = discipline["total"]

    base_100 = round(
        criteria_100 * TERM_WEIGHTS["criteria"]
        + attendance_100 * TERM_WEIGHTS["attendance"]
        + da_100 * TERM_WEIGHTS["data_analysis"]
        + homework["percent"] * TERM_WEIGHTS["homework"]
        + exams["percent"] * TERM_WEIGHTS["exams"]
        + float(conduct_points), 1,
    )
    base_ib = min(8.0, round(base_100 / 12.5, 1))
    computed_100 = max(0.0, min(100.0, round(base_100 + discipline_points, 1)))
    computed_ib = max(0.0, min(8.0, round(base_ib + discipline_points / 12.5, 1)))

    override = db.execute(
        "SELECT * FROM term_grade_overrides WHERE term_id = ? AND student_id = ? AND lesson_id = ?",
        (term_id, student_id, lesson_id),
    ).fetchone()
    override_base_100 = _round1(override["final_100"]) if override and override["final_100"] is not None else base_100
    override_base_ib = _round1(override["ib_score"]) if override and override["ib_score"] is not None else base_ib
    final_100 = max(0.0, min(100.0, round(override_base_100 + discipline_points, 1)))
    final_ib = max(0.0, min(8.0, round(override_base_ib + discipline_points / 12.5, 1)))

    return {
        "sessions": session_count,
        "attendance_points": _round1(attendance_points),
        "attendance_max": setting["total_points"],
        "attendance_100": attendance_100,
        "attendance_ib": min(8.0, round(attendance_100 / 12.5, 1)),
        "criteria_raw": _round1(criteria_sum),
        "criteria_ib": criteria_ib,
        "criteria_100": criteria_100,
        "conduct_points": _round1(conduct_points),
        "discipline_points": discipline_points,
        "discipline_activity": discipline["activity"],
        "discipline_penalty": discipline["penalty"],
        "discipline_count": discipline["count"],
        "da_score": _round1(da_score),
        "da_max": da_max,
        "da_100": da_100,
        "da_ib": da_ib,
        "homework_score": homework["score"],
        "homework_max": homework["max"],
        "homework_count": homework["count"],
        "homework_100": homework["percent"],
        "exam_average": exams["average"],
        "exam_count": exams["count"],
        "exam_100": exams["percent"],
        "base_100": base_100,
        "base_ib": base_ib,
        "override_base_100": override_base_100,
        "override_base_ib": override_base_ib,
        "computed_100": computed_100,
        "computed_ib": computed_ib,
        "final_100": final_100,
        "final_ib": final_ib,
        "overridden": bool(override and override["final_100"] is not None),
        "override_note": (override["note"] if override else "") or "",
    }


def _json_object(value) -> dict:
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def _ts_day(value) -> str:
    try:
        return datetime.fromtimestamp(int(value)).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError):
        return ""


def _in_term(day: str, term: dict | None) -> bool:
    if not term:
        return True
    if not day:
        return True
    start = term.get("start_date") or "0000-01-01"
    end = term.get("end_date") or "9999-12-31"
    return start <= day <= end


def _term_report(db: sqlite3.Connection, student_id: int, term_id: Optional[int] = None) -> dict | None:
    student = db.execute(
        "SELECT * FROM users WHERE id = ? AND role = 'student'", (student_id,)
    ).fetchone()
    if not student:
        return None
    student = dict(student)

    terms = _all_terms(db)
    term = next((t for t in terms if t["id"] == term_id), None)
    if not term:
        term = next((t for t in terms if t["is_active"]), terms[0] if terms else None)
    term = dict(term) if term else {
        "id": 0, "name": "Бүх хугацаа", "school_year": "", "start_date": "", "end_date": "",
    }

    grade_id = _grade_id_by_name(db, student.get("grade") or "")
    lessons = []
    for lesson in _visible_lessons_for_student(db, student.get("grade") or ""):
        breakdown = _term_grade_breakdown(
            db, term["id"], student_id, lesson["id"], grade_id or 0
        )
        lessons.append({
            "id": lesson["id"], "title": lesson["title"] or "", "breakdown": breakdown,
        })
    if lessons:
        overall_100 = _round1(sum(l["breakdown"]["final_100"] for l in lessons) / len(lessons))
        overall_ib = _round1(sum(l["breakdown"]["final_ib"] for l in lessons) / len(lessons))
    else:
        overall_100, overall_ib = 0.0, 0.0

    attendance_sessions = [dict(r) for r in db.execute(
        """SELECT ls.date, ls.topic, l.title AS lesson_title, am.status, am.points, am.note
           FROM attendance_marks am
           JOIN lesson_sessions ls ON ls.id = am.session_id
           LEFT JOIN lessons l ON l.id = ls.lesson_id
           WHERE am.student_id = ? AND ls.term_id = ?
           ORDER BY ls.date, ls.id""",
        (student_id, term["id"]),
    ).fetchall()]
    daily_attendance = [dict(r) for r in db.execute(
        """SELECT date, status, score, note FROM attendance_records
           WHERE student_id = ? AND date >= ? AND date <= ?
           ORDER BY date DESC""",
        (student_id, term.get("start_date") or "0000-01-01", term.get("end_date") or "9999-12-31"),
    ).fetchall()]
    criteria = [dict(r) for r in db.execute(
        """SELECT ls.date, l.title AS lesson_title, cs.criterion, cs.score, cs.note
           FROM criteria_scores cs
           JOIN lesson_sessions ls ON ls.id = cs.session_id
           LEFT JOIN lessons l ON l.id = ls.lesson_id
           WHERE cs.student_id = ? AND ls.term_id = ?
           ORDER BY ls.date, cs.criterion""",
        (student_id, term["id"]),
    ).fetchall()]
    conduct = [dict(r) for r in db.execute(
        """SELECT ls.date, l.title AS lesson_title, lc.points, lc.note
           FROM lesson_conduct_records lc
           JOIN lesson_sessions ls ON ls.id = lc.session_id
           LEFT JOIN lessons l ON l.id = ls.lesson_id
           WHERE lc.student_id = ? AND ls.term_id = ? AND lc.points < 0
           ORDER BY ls.date""",
        (student_id, term["id"]),
    ).fetchall()]

    discipline_rows = [dict(r) for r in db.execute(
        """SELECT date, note, score_delta, photo_path, source FROM discipline_records
           WHERE student_id = ? AND date >= ? AND date <= ?
           ORDER BY date DESC, id DESC""",
        (student_id, term.get("start_date") or "0000-01-01", term.get("end_date") or "9999-12-31"),
    ).fetchall()]
    for row in discipline_rows:
        row["photo_url"] = _photo_url(row["photo_path"])
        row["photo_file"] = _photo_file(row["photo_path"])
    activity = [r for r in discipline_rows if float(r["score_delta"] or 0) > 0]
    discipline = [r for r in discipline_rows if float(r["score_delta"] or 0) < 0]

    homework = []
    for row in db.execute(
        """SELECT h.title, h.due_date, hs.score, hs.max_score, hs.status, hs.explanation,
                  hs.submitted_at, hs.checked_at
           FROM homework_submissions hs
           JOIN homework h ON h.id = hs.homework_id
           WHERE hs.student_id = ? ORDER BY h.due_date, h.id""",
        (student_id,),
    ).fetchall():
        item = dict(row)
        item["day"] = item["due_date"] or _ts_day(item["submitted_at"])
        if _in_term(item["day"], term):
            homework.append(item)

    exams = []
    for row in db.execute(
        """SELECT q.title AS quiz_title, l.title AS lesson_title, s.total_score,
                  s.evaluation_json, COALESCE(qa.submitted_at, qa.started_at) AS attempt_at
           FROM submissions s
           JOIN quizzes q ON q.id = s.quiz_id
           LEFT JOIN lessons l ON l.id = q.lesson_id
           LEFT JOIN quiz_attempts qa ON qa.quiz_id = s.quiz_id AND qa.student_id = s.student_id
           WHERE s.student_id = ? ORDER BY attempt_at DESC, s.id DESC""",
        (student_id,),
    ).fetchall():
        item = dict(row)
        evaluation = _json_object(item.pop("evaluation_json", None))
        item["day"] = _ts_day(item["attempt_at"])
        item["comment"] = str(evaluation.get("overall_summary") or "")
        if _in_term(item["day"], term):
            exams.append(item)

    data_analysis = [dict(r) for r in db.execute(
        """SELECT d.title, s.score, s.ib_score, s.feedback, s.status
           FROM data_analysis_submissions s
           JOIN data_analysis_assignments d ON d.id = s.assignment_id
           WHERE s.student_id = ? AND d.term_id = ? ORDER BY d.title""",
        (student_id, term["id"]),
    ).fetchall()]

    comments = []
    for lesson in lessons:
        if lesson["breakdown"]["override_note"]:
            comments.append({"source": lesson["title"], "date": term["name"],
                             "text": lesson["breakdown"]["override_note"]})
    for row in criteria:
        if row["note"]:
            comments.append({"source": f"{row['lesson_title']} · Criterion {row['criterion']}",
                             "date": row["date"] or "", "text": row["note"]})
    for row in attendance_sessions + daily_attendance:
        if row.get("note"):
            comments.append({"source": row.get("lesson_title") or "Ирц",
                             "date": row.get("date") or "", "text": row["note"]})
    for row in conduct:
        if row["note"]:
            comments.append({"source": row["lesson_title"] or "Сахилга",
                             "date": row["date"] or "", "text": row["note"]})
    for row in discipline_rows:
        if row["note"]:
            comments.append({"source": "Сахилга / идэвх", "date": row["date"] or "",
                             "text": row["note"]})
    for row in homework:
        if row["explanation"]:
            comments.append({"source": row["title"] or "Гэрийн даалгавар",
                             "date": row["day"] or "", "text": row["explanation"]})
    for row in exams:
        if row["comment"]:
            comments.append({"source": row["quiz_title"] or "Шалгалт",
                             "date": row["day"] or "", "text": row["comment"]})
    for row in data_analysis:
        if row["feedback"]:
            comments.append({"source": row["title"] or "Data analysis", "date": "",
                             "text": row["feedback"]})

    summary = {
        "present": sum(1 for r in attendance_sessions if r["status"] == "present"),
        "late": sum(1 for r in attendance_sessions if r["status"] == "late"),
        "absent": sum(1 for r in attendance_sessions if r["status"] == "absent"),
        "attendance_points": _round1(sum(float(r["points"] or 0) for r in attendance_sessions)),
        "activity_total": _round1(sum(float(r["score_delta"] or 0) for r in activity)),
        "discipline_total": _round1(sum(float(r["score_delta"] or 0) for r in discipline)),
        "homework_total": _round1(sum(float(r["score"] or 0) for r in homework)),
        "exam_count": len(exams),
        "exam_average": _round1(
            sum(float(r["total_score"] or 0) for r in exams) / len(exams)
        ) if exams else 0.0,
    }

    return {
        "student": student,
        "term": term,
        "terms": terms,
        "lessons": lessons,
        "overall_100": overall_100,
        "overall_ib": overall_ib,
        "summary": summary,
        "attendance_sessions": attendance_sessions,
        "daily_attendance": daily_attendance,
        "criteria": criteria,
        "conduct": conduct,
        "activity": activity,
        "discipline": discipline,
        "homework": homework,
        "exams": exams,
        "data_analysis": data_analysis,
        "comments": comments,
        "photo_url": _photo_url(student.get("photo_path")),
    }


def _report_pdf_fonts() -> tuple[str, str | None]:
    candidates = (
        (os.path.join(BASE_DIR, "fonts", "DejaVuSans.ttf"),
         os.path.join(BASE_DIR, "fonts", "DejaVuSans-Bold.ttf")),
        ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf"),
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        ("/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
         "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"),
    )
    for regular, bold in candidates:
        if os.path.isfile(regular):
            return regular, bold if bold and os.path.isfile(bold) else None
    raise RuntimeError("PDF тайлангийн Unicode фонт олдсонгүй (arial.ttf эсвэл DejaVuSans.ttf).")


def _report_pdf(report: dict) -> bytes:
    from fpdf import FPDF

    regular, bold = _report_pdf_fonts()
    pdf = FPDF(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.set_margins(15, 15, 15)
    pdf.add_font("Report", "", regular)
    if bold:
        pdf.add_font("Report", "B", bold)
    pdf.add_page()
    pdf.set_font("Report", "B", 16)
    pdf.cell(0, 9, "EduPath - Сурагчийн улирлын тайлан", ln=1)
    pdf.set_font("Report", "", 10)
    pdf.cell(0, 6, f"{report['term']['name']}  {report['term']['school_year']}", ln=1)
    pdf.ln(2)

    student = report["student"]
    photo_file = _photo_file(student.get("photo_path"))
    info_y = pdf.get_y()
    if photo_file:
        try:
            pdf.image(photo_file, x=15, y=info_y, w=32)
        except (RuntimeError, ValueError):
            photo_file = None
    text_x = 52 if photo_file else 15
    lines = [
        f"Нэр: {student.get('surname') or ''} {student.get('name') or student.get('username') or ''}".strip(),
        f"Оюутны код: {student.get('student_code') or student.get('username') or '-'}",
        f"Анги: {student.get('grade') or '-'}    Хүйс: {student.get('gender') or '-'}",
        f"И-мэйл: {student.get('email') or '-'}    Утас: {student.get('phone') or '-'}",
    ]
    pdf.set_xy(text_x, info_y)
    for line in lines:
        pdf.set_x(text_x)
        pdf.cell(135, 6, line, ln=1)
    pdf.set_y(info_y + max(34, 6 * len(lines) + 2))

    def heading(text: str):
        if pdf.get_y() > 245:
            pdf.add_page()
        pdf.set_font("Report", "B", 12)
        pdf.set_fill_color(113, 75, 103)
        pdf.set_text_color(255, 255, 255)
        pdf.cell(0, 7, f" {text}", ln=1, fill=True)
        pdf.set_text_color(0, 0, 0)
        pdf.set_font("Report", "", 9)
        pdf.ln(1)

    def table(headers: list[str], rows: list[list], widths: list[str | int]):
        if not rows:
            pdf.set_text_color(120, 120, 120)
            pdf.cell(0, 5, "Бүртгэл байхгүй.", ln=1)
            pdf.set_text_color(0, 0, 0)
            pdf.ln(2)
            return
        widths_mm = [float(w) for w in widths]

        def row(cells: list, is_header: bool = False):
            if pdf.get_y() > 265:
                pdf.add_page()
            x, y = pdf.get_x(), pdf.get_y()
            pdf.set_font("Report", "B" if is_header else "", 8 if is_header else 8)
            pdf.set_fill_color(235, 230, 235) if is_header else None
            height = 6.0
            for value, width in zip(cells, widths_mm):
                pdf.set_xy(x, y)
                pdf.multi_cell(width, height, str(value if value not in (None, "") else "-"),
                               border=1, fill=is_header, align="L")
                height = max(height, pdf.get_y() - y)
            pdf.set_xy(x, y + height)
            pdf.set_font("Report", "", 9)

        row(headers, True)
        for values in rows:
            row(values)

    summary = report["summary"]
    heading("Нийт дүн")
    table(
        ["Нийт 100", "Нийт IB", "Ирц", "Идэвх", "Сахилга", "Даалгавар", "Шалгалтын дундаж"],
        [[report["overall_100"], report["overall_ib"],
          f"{summary['present']}/{summary['present'] + summary['late'] + summary['absent']}",
          f"+{summary['activity_total']}", summary["discipline_total"],
          summary["homework_total"], summary["exam_average"]]],
        [20, 16, 18, 18, 20, 24, 34],
    )
    pdf.ln(2)

    heading("Хичээл тус бүрийн улирлын дүн")
    lesson_rows = []
    for item in report["lessons"]:
        b = item["breakdown"]
        lesson_rows.append([
            item["title"], f"{b['attendance_points']}/{b['attendance_max']}",
            f"{b['criteria_ib']}/8", f"{b['homework_100']}%", f"{b['exam_average']}/8",
            f"{b['da_score']}/{b['da_max']}",
            f"+{b['discipline_activity']} / {b['discipline_penalty']}",
            b["final_100"], b["final_ib"], b["override_note"],
        ])
    table(["Хичээл", "Ирц", "A-D", "Даалгавар", "Шалгалт", "DA", "Идэвх/Хасалт",
           "Эцсийн 100", "IB", "Коммент"],
          lesson_rows, [30, 16, 12, 16, 14, 14, 16, 16, 10, 26])
    pdf.ln(2)

    heading("Хичээлийн ирц")
    table(["Огноо", "Хичээл", "Сэдэв", "Ирц", "Оноо", "Тэмдэглэл"],
          [[r["date"], r["lesson_title"], r["topic"], r["status"], r["points"], r["note"]]
           for r in report["attendance_sessions"]],
          [20, 34, 40, 16, 12, 48])
    pdf.ln(2)

    heading("Идэвх / нэмэлт оноо")
    table(["Огноо", "Тайлбар", "Оноо"],
          [[r["date"], r["note"], f"+{r['score_delta']}"] for r in report["activity"]],
          [24, 126, 20])
    pdf.ln(2)

    heading(f"Сахилга бат (хасалт, баримт зурагтай: {sum(1 for r in report['discipline'] if r.get('photo_file'))})")
    table(["Огноо", "Тайлбар", "Оноо", "Баримт зураг"],
          [[r["date"], r["note"], r.get("score_delta", r.get("points", "")),
            "тийм" if r.get("photo_file") else "-"]
           for r in report["discipline"] + report["conduct"]],
          [24, 106, 18, 22])
    for row in report["discipline"]:
        if not row["photo_file"]:
            continue
        if pdf.get_y() > 220:
            pdf.add_page()
        pdf.set_font("Report", "B", 9)
        pdf.cell(0, 6, f"Баримт: {row['date']} - {row['note']}", ln=1)
        pdf.set_font("Report", "", 9)
        y = pdf.get_y()
        try:
            pdf.image(row["photo_file"], x=15, y=y, w=55)
            pdf.set_y(y + 45)
        except (RuntimeError, ValueError):
            pass
    pdf.ln(2)

    heading("Гэрийн даалгавар")
    table(["Огноо", "Даалгавар", "Оноо", "Багшийн коммент"],
          [[r["day"], r["title"], f"{r['score']}/{r['max_score']}", r["explanation"]]
           for r in report["homework"]],
          [22, 48, 20, 80])
    pdf.ln(2)

    heading("Шалгалт (IB A-D)")
    table(["Огноо", "Шалгалт", "Хичээл", "Оноо /8", "Коммент"],
          [[r["day"], r["quiz_title"], r["lesson_title"], r["total_score"], r["comment"]]
           for r in report["exams"]],
          [20, 42, 36, 18, 54])
    pdf.ln(2)

    heading("A-D шалгуур (хичээлээр)")
    table(["Огноо", "Хичээл", "Шалгуур", "Оноо", "Тэмдэглэл"],
          [[r["date"], r["lesson_title"], r["criterion"], r["score"], r["note"]]
           for r in report["criteria"]],
          [22, 44, 20, 16, 68])
    pdf.ln(2)

    if report["data_analysis"]:
        heading("Data analysis")
        table(["Даалгавар", "Оноо", "IB", "Санал"],
              [[r["title"], r["score"], r["ib_score"], r["feedback"]]
               for r in report["data_analysis"]],
              [40, 18, 14, 98])
        pdf.ln(2)

    heading("Багшийн комментууд")
    table(["Эх сурвалж", "Огноо", "Коммент"],
          [[r["source"], r["date"], r["text"]] for r in report["comments"]],
          [48, 24, 98])

    return bytes(pdf.output())


def _generate_sessions(db: sqlite3.Connection, term_id: int, created_by: int) -> int:
    term = db.execute("SELECT * FROM academic_terms WHERE id = ?", (term_id,)).fetchone()
    if not term:
        return 0
    start, end = _parse_date(term["start_date"]), _parse_date(term["end_date"])
    if not start or not end or end < start:
        return 0
    slots = db.execute(
        "SELECT * FROM lesson_timetables WHERE term_id = ? ORDER BY id", (term_id,)
    ).fetchall()
    created = 0
    for slot in slots:
        offset = (int(slot["weekday"]) - start.weekday()) % 7
        first = start + timedelta(days=offset)
        current = first
        while current <= end:
            cur = current.isoformat()
            exists = db.execute(
                """SELECT 1 FROM lesson_sessions
                   WHERE term_id = ? AND lesson_id = ? AND grade_id = ? AND date = ?""",
                (term_id, slot["lesson_id"], slot["grade_id"], cur),
            ).fetchone()
            if not exists:
                db.execute(
                    """INSERT INTO lesson_sessions
                       (term_id, lesson_id, grade_id, date, topic, created_by, created_at)
                       VALUES (?, ?, ?, ?, NULL, ?, ?)""",
                    (term_id, slot["lesson_id"], slot["grade_id"], cur, created_by, int(time.time())),
                )
                created += 1
            current += timedelta(days=7)
    db.commit()
    return created


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


@app.get("/teacher/topic-quizzes", response_class=HTMLResponse)
def topic_quizzes_page(
    request: Request,
    user: dict = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    if not user or user["role"] != "teacher":
        raise HTTPException(status_code=403)
    lessons = db.execute("SELECT id, title FROM lessons ORDER BY title").fetchall()
    topics = [
        {
            "key": key,
            "title": value["title"],
            "description": value["description"],
            "question_count": len(value["questions"]),
            "option_count": len(value["questions"][0]["options"]),
        }
        for key, value in TOPIC_QUIZZES.items()
    ]
    return templates.TemplateResponse(
        request=request,
        name="teacher_topic_quizzes.html",
        context={
            "user": user,
            "lessons": lessons,
            "topics": topics,
            "created": request.query_params.get("created"),
            "error": request.query_params.get("error"),
        },
    )


@app.post("/teacher/topic-quizzes/create")
def create_topic_quiz(
    topic: str = Form(...),
    lesson_id: int = Form(...),
    quiz_title: str = Form(""),
    time_limit_minutes: Optional[str] = Form(None),
    user: dict = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    if not user or user["role"] != "teacher":
        raise HTTPException(status_code=403)
    if topic not in TOPIC_QUIZZES:
        return RedirectResponse("/teacher/topic-quizzes?error=unknown_topic", status_code=status.HTTP_302_FOUND)

    c = db.cursor()
    c.execute("SELECT id FROM lessons WHERE id = ?", (lesson_id,))
    if not c.fetchone():
        return RedirectResponse("/teacher/topic-quizzes?error=invalid_lesson", status_code=status.HTTP_302_FOUND)

    built = build_topic_quiz(topic)
    title = (quiz_title or "").strip() or built["quiz_title"]
    tl = None
    if time_limit_minutes is not None and str(time_limit_minutes).strip():
        try:
            tl = int(str(time_limit_minutes).strip())
            if tl < 1 or tl > 600:
                tl = None
        except ValueError:
            tl = None

    c.execute(
        "INSERT INTO quizzes (lesson_id, title, questions_json, image_path, created_by, is_active, time_limit_minutes) "
        "VALUES (?, ?, ?, NULL, ?, 1, ?)",
        (lesson_id, title, json.dumps(built), user["id"], tl),
    )
    db.commit()
    return RedirectResponse("/teacher/topic-quizzes?created=1", status_code=status.HTTP_302_FOUND)


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


@app.post("/teacher/students/{student_id}/edit")
async def teacher_student_edit(student_id: int, request: Request,
                               user: dict = Depends(get_current_user),
                               db: sqlite3.Connection = Depends(get_db)):
    if not user or user["role"] != "teacher":
        raise HTTPException(status_code=403)
    if not db.execute("SELECT 1 FROM users WHERE id = ? AND role = 'student'", (student_id,)).fetchone():
        raise HTTPException(status_code=404)
    form = await request.form()
    grade = str(form.get("grade") or "").strip().lower()
    if not parse_grade(grade):
        return RedirectResponse("/scores?error=invalid_grade", status_code=status.HTTP_302_FOUND)
    values = {
        "username": str(form.get("username") or "").strip(),
        "student_code": str(form.get("student_code") or "").strip(),
        "name": str(form.get("name") or "").strip(),
        "surname": str(form.get("surname") or "").strip(),
        "grade": grade,
        "email": str(form.get("email") or "").strip().lower(),
        "gender": str(form.get("gender") or "").strip(),
        "phone": str(form.get("phone") or "").strip(),
        "instagram": str(form.get("instagram") or "").strip() or None,
    }
    required = [values["username"], values["student_code"], values["name"], values["surname"],
                values["grade"], values["email"], values["gender"], values["phone"]]
    if not all(required):
        return RedirectResponse("/scores?error=missing_fields", status_code=status.HTTP_302_FOUND)
    password = str(form.get("password") or "").strip()
    assignments = ", ".join(f"{key} = ?" for key in values)
    params = [*values.values()]
    if password:
        assignments += ", password = ?"
        params.append(password)
    params.append(student_id)
    try:
        db.execute(f"UPDATE users SET {assignments} WHERE id = ?", params)
        db.commit()
    except sqlite3.IntegrityError:
        return RedirectResponse("/scores?error=exists", status_code=status.HTTP_302_FOUND)
    return RedirectResponse("/scores?updated=1", status_code=status.HTTP_302_FOUND)


@app.post("/teacher/submissions/{submission_id}/edit")
def teacher_submission_edit(
    submission_id: int, total_score: str = Form(...), user: dict = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    if not user or user["role"] != "teacher":
        raise HTTPException(status_code=403)
    db.execute("UPDATE submissions SET total_score = ? WHERE id = ?",
               (_parse_int(total_score, 0, 0, 8), submission_id))
    db.commit()
    return RedirectResponse("/scores?updated=1", status_code=status.HTTP_302_FOUND)


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


def _clean_google_form_url(value) -> Optional[str]:
    url = str(value or "").strip()
    if not url:
        return None
    if re.match(r"^docs\.google\.com/", url, re.IGNORECASE):
        url = "https://" + url
    if not re.match(r"^https?://", url, re.IGNORECASE):
        return None
    return url


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
        "google_form_url": _clean_google_form_url(form.get("google_form_url")),
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


def _decode_uploaded_csv(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _form_key(value) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _form_column(headers, keywords) -> Optional[str]:
    for header in headers:
        lowered = _form_key(header)
        if any(keyword in lowered for keyword in keywords):
            return header
    return None


def _form_score(value, max_score: int) -> Optional[int]:
    text = str(value or "").strip()
    if not text:
        return None
    fraction = re.search(r"(-?\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)", text)
    if fraction:
        try:
            raw = float(fraction.group(1))
            denominator = float(fraction.group(2))
            if denominator > 0:
                return int(round(min(max_score, max(0, raw / denominator * max_score))))
        except (TypeError, ValueError, ZeroDivisionError):
            pass
    percent = re.search(r"(-?\d+(?:\.\d+)?)\s*%", text)
    if percent:
        return int(round(min(max_score, max(0, float(percent.group(1)) / 100 * max_score))))
    number = re.search(r"-?\d+(?:\.\d+)?", text)
    if number:
        return int(round(min(max_score, max(0, float(number.group(0))))))
    return None


def _form_answers_score(row, answer_columns, max_score: int) -> Optional[int]:
    correct = 0
    total = 0
    positive = {"correct", "зөв", "true", "1", "yes", "тийм"}
    negative = {"incorrect", "буруу", "false", "0", "no", "үгүй"}
    for header in answer_columns:
        value = _form_key(row.get(header))
        if value in positive:
            correct += 1
            total += 1
        elif value in negative:
            total += 1
    if not total:
        return None
    return int(round(correct / total * max_score))


def _form_timestamp(value) -> int:
    text = str(value or "").strip()
    if not text:
        return int(time.time())
    for pattern in (
        "%m/%d/%Y %H:%M:%S",
        "%m/%d/%Y %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%d.%m.%Y %H:%M:%S",
        "%d.%m.%Y %H:%M",
    ):
        try:
            return int(datetime.strptime(text, pattern).timestamp())
        except ValueError:
            continue
    return int(time.time())


def _import_google_form_rows(db: sqlite3.Connection, homework, rows: list[dict]) -> tuple[int, list[str], int]:
    headers = [header for header in (rows[0].keys() if rows else []) if header]
    email_col = _form_column(headers, ("email", "e-mail", "имэйл", "mail"))
    code_col = _form_column(headers, ("student code", "student_code", "student id", "student_id", "сурагчийн код", "код"))
    username_col = _form_column(headers, ("username", "нэвтрэх нэр"))
    name_col = _form_column(headers, ("student name", "full name", "нэр", "name"))
    score_col = _form_column(headers, ("score", "он", "дүн", "point", "result"))
    timestamp_col = _form_column(headers, ("timestamp", "огноо", "цаг", "date", "time"))

    identity_cols = {email_col, code_col, username_col, name_col, score_col, timestamp_col}
    answer_columns = [header for header in headers if header not in identity_cols]

    students = db.execute(
        "SELECT id, email, student_code, username, name, surname FROM users WHERE role = 'student'"
    ).fetchall()
    by_email = {}
    by_code = {}
    by_username = {}
    by_full_name = {}
    for student in students:
        if student["email"]:
            by_email.setdefault(_form_key(student["email"]), student["id"])
        if student["student_code"]:
            by_code.setdefault(_form_key(student["student_code"]), student["id"])
        if student["username"]:
            by_username.setdefault(_form_key(student["username"]), student["id"])
        full_name = _form_key(f"{student['surname'] or ''} {student['name'] or ''}")
        if full_name.strip():
            by_full_name.setdefault(full_name, []).append(student["id"])

    max_score = int(homework["max_score"] or DEFAULT_MAX_SCORE)
    imported = 0
    unmatched = []
    no_score = 0
    now = int(time.time())

    for row in rows:
        student_id = None
        matched_email = str(row.get(email_col) or "").strip() if email_col else ""
        email = _form_key(matched_email)
        code = _form_key(row.get(code_col)) if code_col else ""
        username = _form_key(row.get(username_col)) if username_col else ""
        name = _form_key(row.get(name_col)) if name_col else ""
        for lookup, key in ((by_email, email), (by_code, code), (by_username, username)):
            if key and key in lookup:
                student_id = lookup[key]
                break
        if not student_id and name and name in by_full_name and len(by_full_name[name]) == 1:
            student_id = by_full_name[name][0]
        if not student_id:
            unmatched.append(str(row.get(name_col) or row.get(email_col) or "?").strip() or "?")
            continue

        score = _form_score(row.get(score_col), max_score) if score_col else None
        if score is None:
            score = _form_answers_score(row, answer_columns, max_score)
        if score is None:
            no_score += 1
            continue

        answers = [
            {"question": header, "answer": str(row.get(header) or "").strip()}
            for header in answer_columns
            if str(row.get(header) or "").strip()
        ]
        details = {
            "source": "google_forms",
            "answers": answers,
            "matched_email": matched_email,
            "imported_at": now,
        }
        explanation = "Google Forms-оос импортолсон оноо."
        if score_col and str(row.get(score_col) or "").strip():
            explanation += f" Формын оноо: {str(row.get(score_col)).strip()}."
        submitted_at = _form_timestamp(row.get(timestamp_col)) if timestamp_col else now
        code_text = "\n".join(f"{item['question']}: {item['answer']}" for item in answers) or "Google Forms"

        existing = db.execute(
            "SELECT id FROM homework_submissions WHERE homework_id = ? AND student_id = ?",
            (homework["id"], student_id),
        ).fetchone()
        if existing:
            db.execute(
                """UPDATE homework_submissions
                   SET code = ?, status = 'done', score = ?, max_score = ?, explanation = ?,
                       test_results_json = ?, submitted_at = ?, checked_at = ?
                   WHERE id = ?""",
                (code_text, score, max_score, explanation, json.dumps(details), submitted_at, now, existing["id"]),
            )
        else:
            db.execute(
                """INSERT INTO homework_submissions
                   (homework_id, student_id, code, status, score, max_score, explanation, test_results_json, submitted_at, checked_at)
                   VALUES (?, ?, ?, 'done', ?, ?, ?, ?, ?, ?)""",
                (homework["id"], student_id, code_text, score, max_score, explanation,
                 json.dumps(details), submitted_at, now),
            )
        db.execute(
            "DELETE FROM google_form_results WHERE homework_id = ? AND student_id = ?",
            (homework["id"], student_id),
        )
        db.execute(
            """INSERT INTO google_form_results
               (homework_id, student_id, student_email, score, max_score, source_row_json, imported_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (homework["id"], student_id, matched_email, score, max_score,
             json.dumps(row, ensure_ascii=False), now),
        )
        imported += 1
    return imported, unmatched, no_score


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
             google_form_url, created_by, created_at, is_active)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
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
            data["google_form_url"],
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
            "gf_imported": request.query_params.get("gf_imported"),
            "gf_unmatched": request.query_params.get("gf_unmatched"),
            "gf_noscore": request.query_params.get("gf_noscore"),
            "gf_error": request.query_params.get("gf_error"),
        },
    )


@app.post("/teacher/homework/{homework_id}/google-forms/import")
async def teacher_homework_import_google_forms(
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
    raw = b""
    csv_file = form.get("csv_file")
    if csv_file and getattr(csv_file, "filename", None):
        raw = await csv_file.read()
    text = _decode_uploaded_csv(raw) if raw else str(form.get("csv_text") or "")
    if not text.strip():
        return RedirectResponse(
            f"/teacher/homework/{homework_id}?gf_error=no_data",
            status_code=status.HTTP_302_FOUND,
        )
    try:
        rows = [
            {key: value for key, value in row.items() if key}
            for row in csv.DictReader(io.StringIO(text))
        ]
    except Exception:
        return RedirectResponse(
            f"/teacher/homework/{homework_id}?gf_error=bad_csv",
            status_code=status.HTTP_302_FOUND,
        )
    if not rows:
        return RedirectResponse(
            f"/teacher/homework/{homework_id}?gf_error=no_rows",
            status_code=status.HTTP_302_FOUND,
        )

    imported, unmatched, no_score = _import_google_form_rows(db, homework, rows)
    db.commit()
    return RedirectResponse(
        f"/teacher/homework/{homework_id}?gf_imported={imported}&gf_unmatched={len(unmatched)}&gf_noscore={no_score}",
        status_code=status.HTTP_302_FOUND,
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
               check_mode = ?, check_type = ?, fixed_answer = ?, google_form_url = ?
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
            data["google_form_url"],
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
    photo_path = _save_evidence_photo(form.get("evidence_photo"))
    c.execute(
        """INSERT INTO discipline_records
           (student_id, grade, date, note, score_delta, photo_path, source, created_by, created_at)
           VALUES (?, ?, ?, ?, ?, ?, 'manual', ?, ?)""",
        (student_id, grade, selected_date, note, delta, photo_path, user["id"], int(time.time())),
    )
    db.commit()
    return RedirectResponse(
        f"/teacher/discipline?grade={grade}&date={selected_date}&saved=1",
        status_code=status.HTTP_302_FOUND,
    )


@app.post("/teacher/discipline/{record_id}/edit")
def teacher_discipline_edit(
    record_id: int, grade: str = Form(""), date: str = Form(""), note: str = Form(""),
    score_delta: str = Form(""), user: dict = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    if not user or user["role"] != "teacher":
        raise HTTPException(status_code=403)
    delta = _clamp_delta(score_delta, -100, 100)
    if delta == 0 or not (note or "").strip():
        return RedirectResponse(
            f"/teacher/discipline?grade={grade}&date={date}&error=invalid",
            status_code=status.HTTP_302_FOUND,
        )
    db.execute(
        "UPDATE discipline_records SET note = ?, score_delta = ? WHERE id = ?",
        ((note or "").strip(), delta, record_id),
    )
    db.commit()
    return RedirectResponse(
        f"/teacher/discipline?grade={grade}&date={date}&saved=1",
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
    lessons = _visible_lessons_for_student(db, user["grade"] or "")
    visible_ids = {lesson["id"] for lesson in lessons}
    c.execute('''
        SELECT quizzes.*, lessons.title as lesson_title,
        (SELECT id FROM submissions WHERE submissions.quiz_id = quizzes.id AND submissions.student_id = ?) as sub_id
        FROM quizzes
        JOIN lessons ON quizzes.lesson_id = lessons.id
        WHERE COALESCE(quizzes.is_active, 1) = 1
        ORDER BY quizzes.id DESC
    ''', (user["id"],))
    quizzes = [row for row in c.fetchall() if row["lesson_id"] in visible_ids]
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


# --- IB TERM SETUP: ANGI, ULIRAL, HICHEEL, UNIT PLAN, HUVAAR ---

def _setup_counts(db: sqlite3.Connection) -> dict:
    counts = {}
    for key, table in (
        ("grades", "grades"), ("terms", "academic_terms"), ("lessons", "lessons"),
        ("unit_plans", "ib_unit_plans"), ("timetable", "lesson_timetables"),
        ("sessions", "lesson_sessions"), ("assignments", "data_analysis_assignments"),
    ):
        counts[key] = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    return counts


@app.get("/teacher/setup", response_class=HTMLResponse)
def teacher_setup_page(request: Request, user: dict = Depends(get_current_user),
                       db: sqlite3.Connection = Depends(get_db)):
    if not user or user["role"] != "teacher":
        return RedirectResponse("/login")
    return templates.TemplateResponse(
        request=request, name="teacher_setup.html",
        context={"user": user, "counts": _setup_counts(db)},
    )


@app.get("/teacher/setup/grades", response_class=HTMLResponse)
def teacher_grades_page(request: Request, user: dict = Depends(get_current_user),
                        db: sqlite3.Connection = Depends(get_db)):
    if not user or user["role"] != "teacher":
        return RedirectResponse("/login")
    rows = db.execute(
        """SELECT g.*, (SELECT COUNT(*) FROM users u
                       WHERE u.role = 'student' AND lower(u.grade) = lower(g.name)) as student_count
           FROM grades g ORDER BY g.level, g.section"""
    ).fetchall()
    return templates.TemplateResponse(
        request=request, name="teacher_grades.html",
        context={"user": user, "grades": [dict(r) for r in rows]},
    )


@app.post("/teacher/setup/grades")
def teacher_grade_create(name: str = Form(...), user: dict = Depends(get_current_user),
                         db: sqlite3.Connection = Depends(get_db)):
    if not user or user["role"] != "teacher":
        raise HTTPException(status_code=403)
    normalized = (name or "").strip().lower()
    parsed = parse_grade(normalized)
    if not parsed:
        return RedirectResponse("/teacher/setup/grades?error=invalid", status_code=status.HTTP_302_FOUND)
    try:
        db.execute(
            "INSERT INTO grades (name, level, section, is_active, created_at) VALUES (?, ?, ?, 1, ?)",
            (normalized, parsed[0], parsed[1], int(time.time())),
        )
        db.commit()
    except sqlite3.IntegrityError:
        return RedirectResponse("/teacher/setup/grades?error=exists", status_code=status.HTTP_302_FOUND)
    return RedirectResponse("/teacher/setup/grades?created=1", status_code=status.HTTP_302_FOUND)


@app.post("/teacher/setup/grades/{grade_id}/edit")
def teacher_grade_edit(grade_id: int, name: str = Form(...), user: dict = Depends(get_current_user),
                       db: sqlite3.Connection = Depends(get_db)):
    if not user or user["role"] != "teacher":
        raise HTTPException(status_code=403)
    old_name = _grade_name(db, grade_id)
    normalized = (name or "").strip().lower()
    parsed = parse_grade(normalized)
    if not parsed or not old_name:
        return RedirectResponse("/teacher/setup/grades?error=invalid", status_code=status.HTTP_302_FOUND)
    try:
        db.execute(
            "UPDATE grades SET name = ?, level = ?, section = ? WHERE id = ?",
            (normalized, parsed[0], parsed[1], grade_id),
        )
    except sqlite3.IntegrityError:
        return RedirectResponse("/teacher/setup/grades?error=exists", status_code=status.HTTP_302_FOUND)
    if normalized != old_name:
        for table in ("users", "attendance_records", "discipline_records"):
            db.execute(f"UPDATE {table} SET grade = ? WHERE lower(grade) = lower(?)",
                       (normalized, old_name))
    db.commit()
    return RedirectResponse("/teacher/setup/grades?updated=1", status_code=status.HTTP_302_FOUND)


@app.post("/teacher/setup/grades/{grade_id}/delete")
def teacher_grade_delete(grade_id: int, user: dict = Depends(get_current_user),
                         db: sqlite3.Connection = Depends(get_db)):
    if not user or user["role"] != "teacher":
        raise HTTPException(status_code=403)
    grade_name = _grade_name(db, grade_id)
    for sql in (
        """DELETE FROM criteria_scores WHERE session_id IN
           (SELECT id FROM lesson_sessions WHERE grade_id = ?)""",
        """DELETE FROM attendance_marks WHERE session_id IN
           (SELECT id FROM lesson_sessions WHERE grade_id = ?)""",
        """DELETE FROM lesson_conduct_records WHERE session_id IN
           (SELECT id FROM lesson_sessions WHERE grade_id = ?)""",
        "DELETE FROM lesson_sessions WHERE grade_id = ?",
        "DELETE FROM lesson_grades WHERE grade_id = ?",
        "DELETE FROM lesson_timetables WHERE grade_id = ?",
        "DELETE FROM term_attendance_config WHERE grade_id = ?",
        """DELETE FROM data_analysis_submissions WHERE assignment_id IN
           (SELECT id FROM data_analysis_assignments WHERE grade_id = ?)""",
        "DELETE FROM data_analysis_assignments WHERE grade_id = ?",
    ):
        db.execute(sql, (grade_id,))
    db.execute(
        "DELETE FROM term_grade_overrides WHERE student_id IN (SELECT id FROM users WHERE lower(grade) = lower(?))",
        (grade_name,),
    )
    db.execute("DELETE FROM grades WHERE id = ?", (grade_id,))
    db.commit()
    return RedirectResponse("/teacher/setup/grades?deleted=1", status_code=status.HTTP_302_FOUND)


@app.get("/teacher/setup/terms", response_class=HTMLResponse)
def teacher_terms_page(request: Request, user: dict = Depends(get_current_user),
                       db: sqlite3.Connection = Depends(get_db)):
    if not user or user["role"] != "teacher":
        return RedirectResponse("/login")
    rows = []
    for term in _all_terms(db):
        item = dict(term)
        item["sessions"] = db.execute(
            "SELECT COUNT(*) FROM lesson_sessions WHERE term_id = ?", (term["id"],)
        ).fetchone()[0]
        rows.append(item)
    return templates.TemplateResponse(
        request=request, name="teacher_terms.html",
        context={"user": user, "terms": rows},
    )


@app.post("/teacher/setup/terms")
def teacher_term_create(
    name: str = Form(...), school_year: str = Form(""), start_date: str = Form(...),
    end_date: str = Form(...), weeks: str = Form(""), user: dict = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    if not user or user["role"] != "teacher":
        raise HTTPException(status_code=403)
    start, end = _parse_date(start_date), _parse_date(end_date)
    if not start or not end or end < start:
        return RedirectResponse("/teacher/setup/terms?error=invalid_dates", status_code=status.HTTP_302_FOUND)
    computed = _weeks_between(start.isoformat(), end.isoformat())
    week_count = _parse_int(weeks, computed, 1, 52) if str(weeks or "").strip() else computed
    year = (school_year or "").strip() or _default_school_year()[0]
    db.execute(
        """INSERT INTO academic_terms (name, school_year, start_date, end_date, weeks, is_active, created_at)
           VALUES (?, ?, ?, ?, ?, 1, ?)""",
        ((name or "").strip(), year, start.isoformat(), end.isoformat(), week_count, int(time.time())),
    )
    db.commit()
    return RedirectResponse("/teacher/setup/terms?created=1", status_code=status.HTTP_302_FOUND)


@app.post("/teacher/setup/terms/{term_id}/edit")
def teacher_term_edit(
    term_id: int, name: str = Form(...), school_year: str = Form(""),
    start_date: str = Form(...), end_date: str = Form(...), weeks: str = Form(""),
    user: dict = Depends(get_current_user), db: sqlite3.Connection = Depends(get_db),
):
    if not user or user["role"] != "teacher":
        raise HTTPException(status_code=403)
    if not db.execute("SELECT 1 FROM academic_terms WHERE id = ?", (term_id,)).fetchone():
        raise HTTPException(status_code=404)
    start, end = _parse_date(start_date), _parse_date(end_date)
    if not start or not end or end < start or not (name or "").strip():
        return RedirectResponse("/teacher/setup/terms?error=invalid_dates", status_code=status.HTTP_302_FOUND)
    computed = _weeks_between(start.isoformat(), end.isoformat())
    week_count = _parse_int(weeks, computed, 1, 52) if str(weeks or "").strip() else computed
    db.execute(
        """UPDATE academic_terms
           SET name = ?, school_year = ?, start_date = ?, end_date = ?, weeks = ?
           WHERE id = ?""",
        ((name or "").strip(), (school_year or "").strip() or _default_school_year()[0],
         start.isoformat(), end.isoformat(), week_count, term_id),
    )
    db.commit()
    return RedirectResponse("/teacher/setup/terms?updated=1", status_code=status.HTTP_302_FOUND)


@app.post("/teacher/setup/terms/{term_id}/delete")
def teacher_term_delete(term_id: int, user: dict = Depends(get_current_user),
                        db: sqlite3.Connection = Depends(get_db)):
    if not user or user["role"] != "teacher":
        raise HTTPException(status_code=403)
    for sql in (
        "DELETE FROM criteria_scores WHERE session_id IN (SELECT id FROM lesson_sessions WHERE term_id = ?)",
        "DELETE FROM attendance_marks WHERE session_id IN (SELECT id FROM lesson_sessions WHERE term_id = ?)",
        "DELETE FROM lesson_conduct_records WHERE session_id IN (SELECT id FROM lesson_sessions WHERE term_id = ?)",
    ):
        db.execute(sql, (term_id,))
    db.execute(
        """DELETE FROM data_analysis_submissions WHERE assignment_id IN
           (SELECT id FROM data_analysis_assignments WHERE term_id = ?)""",
        (term_id,),
    )
    for sql in (
        "DELETE FROM lesson_sessions WHERE term_id = ?",
        "DELETE FROM lesson_timetables WHERE term_id = ?",
        "DELETE FROM term_attendance_config WHERE term_id = ?",
        "DELETE FROM data_analysis_assignments WHERE term_id = ?",
        "DELETE FROM term_grade_overrides WHERE term_id = ?",
        "DELETE FROM academic_terms WHERE id = ?",
    ):
        db.execute(sql, (term_id,))
    db.commit()
    return RedirectResponse("/teacher/setup/terms?deleted=1", status_code=status.HTTP_302_FOUND)


@app.get("/teacher/setup/lessons", response_class=HTMLResponse)
def teacher_lessons_page(request: Request, user: dict = Depends(get_current_user),
                         db: sqlite3.Connection = Depends(get_db)):
    if not user or user["role"] != "teacher":
        return RedirectResponse("/login")
    rows = []
    for lesson in db.execute("SELECT * FROM lessons ORDER BY id DESC").fetchall():
        item = dict(lesson)
        item["teachers"] = [dict(r) for r in db.execute(
            """SELECT u.id, u.name, u.username FROM lesson_teachers lt
               JOIN users u ON u.id = lt.teacher_id WHERE lt.lesson_id = ? ORDER BY u.id""",
            (lesson["id"],),
        ).fetchall()]
        item["grades"] = [r[0] for r in db.execute(
            """SELECT g.name FROM lesson_grades lg JOIN grades g ON g.id = lg.grade_id
               WHERE lg.lesson_id = ? ORDER BY g.level, g.section""",
            (lesson["id"],),
        ).fetchall()]
        item["grade_ids"] = [r[0] for r in db.execute(
            "SELECT grade_id FROM lesson_grades WHERE lesson_id = ?", (lesson["id"],)
        ).fetchall()]
        item["teacher_ids"] = [r[0] for r in db.execute(
            "SELECT teacher_id FROM lesson_teachers WHERE lesson_id = ?", (lesson["id"],)
        ).fetchall()]
        item["unit_plans"] = db.execute(
            "SELECT COUNT(*) FROM ib_unit_plans WHERE lesson_id = ?", (lesson["id"],)
        ).fetchone()[0]
        rows.append(item)
    return templates.TemplateResponse(
        request=request, name="teacher_lessons.html",
        context={
            "user": user, "lessons": rows,
            "teachers": [dict(r) for r in db.execute(
                "SELECT id, name, username FROM users WHERE role = 'teacher' ORDER BY id"
            ).fetchall()],
            "grades": _all_grades(db),
        },
    )


@app.post("/teacher/setup/lessons")
async def teacher_lesson_register(request: Request, user: dict = Depends(get_current_user),
                                  db: sqlite3.Connection = Depends(get_db)):
    if not user or user["role"] != "teacher":
        raise HTTPException(status_code=403)
    form = await request.form()
    title = str(form.get("title") or "").strip()
    lesson_id = _parse_int(form.get("lesson_id"), 0)
    teacher_ids = [_parse_int(v) for v in form.getlist("teacher_ids") if str(v).strip()]
    grade_ids = [_parse_int(v) for v in form.getlist("grade_ids") if str(v).strip()]
    if not lesson_id and not title:
        return RedirectResponse("/teacher/setup/lessons?error=missing_title", status_code=status.HTTP_302_FOUND)
    if not teacher_ids or not grade_ids:
        return RedirectResponse("/teacher/setup/lessons?error=missing_mapping", status_code=status.HTTP_302_FOUND)
    if not lesson_id:
        db.execute(
            "INSERT INTO lessons (title, filename, teacher_id) VALUES (?, NULL, ?)",
            (title, teacher_ids[0]),
        )
        lesson_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    for teacher_id in teacher_ids:
        db.execute("INSERT OR IGNORE INTO lesson_teachers (lesson_id, teacher_id) VALUES (?, ?)",
                   (lesson_id, teacher_id))
    for grade_id in grade_ids:
        db.execute("INSERT OR IGNORE INTO lesson_grades (lesson_id, grade_id) VALUES (?, ?)",
                   (lesson_id, grade_id))
    db.execute("UPDATE lessons SET teacher_id = COALESCE(teacher_id, ?) WHERE id = ?",
               (teacher_ids[0], lesson_id))
    db.commit()
    return RedirectResponse(f"/teacher/setup/lessons/{lesson_id}?created=1", status_code=status.HTTP_302_FOUND)


@app.post("/teacher/setup/lessons/{lesson_id}/edit")
async def teacher_lesson_edit(lesson_id: int, request: Request, user: dict = Depends(get_current_user),
                              db: sqlite3.Connection = Depends(get_db)):
    if not user or user["role"] != "teacher":
        raise HTTPException(status_code=403)
    if not db.execute("SELECT 1 FROM lessons WHERE id = ?", (lesson_id,)).fetchone():
        raise HTTPException(status_code=404)
    form = await request.form()
    title = str(form.get("title") or "").strip()
    teacher_ids = [_parse_int(v) for v in form.getlist("teacher_ids") if str(v).strip()]
    grade_ids = [_parse_int(v) for v in form.getlist("grade_ids") if str(v).strip()]
    if not title or not teacher_ids or not grade_ids:
        return RedirectResponse("/teacher/setup/lessons?error=missing_mapping", status_code=status.HTTP_302_FOUND)
    db.execute("UPDATE lessons SET title = ?, teacher_id = ? WHERE id = ?",
               (title, teacher_ids[0], lesson_id))
    db.execute("DELETE FROM lesson_teachers WHERE lesson_id = ?", (lesson_id,))
    db.execute("DELETE FROM lesson_grades WHERE lesson_id = ?", (lesson_id,))
    for teacher_id in teacher_ids:
        db.execute("INSERT OR IGNORE INTO lesson_teachers (lesson_id, teacher_id) VALUES (?, ?)",
                   (lesson_id, teacher_id))
    for grade_id in grade_ids:
        db.execute("INSERT OR IGNORE INTO lesson_grades (lesson_id, grade_id) VALUES (?, ?)",
                   (lesson_id, grade_id))
    db.commit()
    return RedirectResponse("/teacher/setup/lessons?updated=1", status_code=status.HTTP_302_FOUND)


@app.get("/teacher/setup/lessons/{lesson_id}", response_class=HTMLResponse)
def teacher_lesson_detail(lesson_id: int, request: Request, user: dict = Depends(get_current_user),
                          db: sqlite3.Connection = Depends(get_db)):
    if not user or user["role"] != "teacher":
        return RedirectResponse("/login")
    lesson = db.execute("SELECT * FROM lessons WHERE id = ?", (lesson_id,)).fetchone()
    if not lesson:
        raise HTTPException(status_code=404)
    unit_plans = [dict(r) for r in db.execute(
        "SELECT * FROM ib_unit_plans WHERE lesson_id = ? ORDER BY id DESC", (lesson_id,)
    ).fetchall()]
    sessions = [dict(r) for r in db.execute(
        """SELECT s.*, t.name as term_name, g.name as grade_name
           FROM lesson_sessions s JOIN academic_terms t ON t.id = s.term_id
           JOIN grades g ON g.id = s.grade_id WHERE s.lesson_id = ?
           ORDER BY s.date DESC""",
        (lesson_id,),
    ).fetchall()]
    return templates.TemplateResponse(
        request=request, name="teacher_lesson_detail.html",
        context={
            "user": user, "lesson": dict(lesson), "unit_plans": unit_plans,
            "sessions": sessions, "criteria": CRITERION_LETTERS,
        },
    )


@app.get("/teacher/setup/unit-plans", response_class=HTMLResponse)
def teacher_unit_plans_page(request: Request, user: dict = Depends(get_current_user),
                            db: sqlite3.Connection = Depends(get_db)):
    if not user or user["role"] != "teacher":
        return RedirectResponse("/login")
    plans = [dict(r) for r in db.execute(
        """SELECT p.*, l.title as lesson_title FROM ib_unit_plans p
           JOIN lessons l ON l.id = p.lesson_id ORDER BY p.id DESC"""
    ).fetchall()]
    return templates.TemplateResponse(
        request=request, name="teacher_unit_plans.html",
        context={"user": user, "plans": plans, "lessons": [dict(r) for r in db.execute(
            "SELECT id, title FROM lessons ORDER BY id DESC"
        ).fetchall()]},
    )


@app.post("/teacher/setup/unit-plans")
def teacher_unit_plan_create(
    lesson_id: int = Form(...), title: str = Form(...), unit_code: str = Form(""),
    description: str = Form(""), atl_skills: str = Form(""), max_score: str = Form("8"),
    user: dict = Depends(get_current_user), db: sqlite3.Connection = Depends(get_db),
):
    if not user or user["role"] != "teacher":
        raise HTTPException(status_code=403)
    if not (title or "").strip():
        return RedirectResponse("/teacher/setup/unit-plans?error=missing_title", status_code=status.HTTP_302_FOUND)
    db.execute(
        """INSERT INTO ib_unit_plans
           (lesson_id, title, unit_code, description, atl_skills, max_score, created_by, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (lesson_id, title.strip(), (unit_code or "").strip(), (description or "").strip(),
         (atl_skills or "").strip(), _parse_int(max_score, 8, 1, 100), user["id"], int(time.time())),
    )
    db.commit()
    return RedirectResponse("/teacher/setup/unit-plans?created=1", status_code=status.HTTP_302_FOUND)


@app.post("/teacher/setup/unit-plans/{plan_id}/edit")
def teacher_unit_plan_edit(
    plan_id: int, lesson_id: int = Form(...), title: str = Form(...), unit_code: str = Form(""),
    description: str = Form(""), atl_skills: str = Form(""), max_score: str = Form("8"),
    user: dict = Depends(get_current_user), db: sqlite3.Connection = Depends(get_db),
):
    if not user or user["role"] != "teacher":
        raise HTTPException(status_code=403)
    if not (title or "").strip():
        return RedirectResponse("/teacher/setup/unit-plans?error=missing_title", status_code=status.HTTP_302_FOUND)
    db.execute(
        """UPDATE ib_unit_plans SET lesson_id = ?, title = ?, unit_code = ?, description = ?,
           atl_skills = ?, max_score = ? WHERE id = ?""",
        (lesson_id, title.strip(), (unit_code or "").strip(), (description or "").strip(),
         (atl_skills or "").strip(), _parse_int(max_score, 8, 1, 100), plan_id),
    )
    db.commit()
    return RedirectResponse("/teacher/setup/unit-plans?updated=1", status_code=status.HTTP_302_FOUND)


@app.post("/teacher/setup/unit-plans/{plan_id}/delete")
def teacher_unit_plan_delete(plan_id: int, user: dict = Depends(get_current_user),
                             db: sqlite3.Connection = Depends(get_db)):
    if not user or user["role"] != "teacher":
        raise HTTPException(status_code=403)
    for sql in (
        "UPDATE criteria_scores SET unit_plan_id = NULL WHERE unit_plan_id = ?",
        "UPDATE data_analysis_assignments SET unit_plan_id = NULL WHERE unit_plan_id = ?",
        "DELETE FROM ib_unit_plans WHERE id = ?",
    ):
        db.execute(sql, (plan_id,))
    db.commit()
    return RedirectResponse("/teacher/setup/unit-plans?deleted=1", status_code=status.HTTP_302_FOUND)


@app.get("/teacher/timetable", response_class=HTMLResponse)
def teacher_timetable_page(
    request: Request, term_id: Optional[int] = None, grade_id: Optional[int] = None,
    user: dict = Depends(get_current_user), db: sqlite3.Connection = Depends(get_db),
):
    if not user or user["role"] != "teacher":
        return RedirectResponse("/login")
    terms, grades = _all_terms(db), _all_grades(db)
    selected_term = term_id or next((t["id"] for t in terms if t["is_active"]), terms[0]["id"] if terms else 0)
    selected_grade = grade_id or next((g["id"] for g in grades if g["is_active"]), grades[0]["id"] if grades else 0)
    where, params = "tt.term_id = ?", [selected_term]
    if selected_grade:
        where += " AND tt.grade_id = ?"
        params.append(selected_grade)
    slots = db.execute(
        f"""SELECT tt.*, l.title as lesson_title, g.name as grade_name,
                   COALESCE(u.name, u.username, '') as teacher_name
            FROM lesson_timetables tt
            JOIN lessons l ON l.id = tt.lesson_id
            JOIN grades g ON g.id = tt.grade_id
            LEFT JOIN users u ON u.id = tt.teacher_id
            WHERE {where} ORDER BY tt.weekday, tt.period""",
        params,
    ).fetchall()
    grid = {}
    for slot in slots:
        grid.setdefault((slot["weekday"], slot["period"]), []).append(dict(slot))
    lessons = [dict(r) for r in db.execute("""SELECT DISTINCT l.id, l.title FROM lessons l
        JOIN lesson_grades lg ON lg.lesson_id = l.id WHERE lg.grade_id = ? ORDER BY l.title""",
        (selected_grade,)).fetchall()]
    return templates.TemplateResponse(
        request=request, name="teacher_timetable.html",
        context={
            "user": user, "terms": terms, "grades": grades, "lessons": lessons,
            "teachers": [dict(r) for r in db.execute(
                "SELECT id, name, username FROM users WHERE role = 'teacher' ORDER BY id"
            ).fetchall()],
            "selected_term": selected_term, "selected_grade": selected_grade,
            "weekdays": WEEKDAY_NAMES, "periods": list(range(1, 9)), "grid": grid,
        },
    )


@app.post("/teacher/timetable")
def teacher_timetable_create(
    term_id: int = Form(...), grade_id: int = Form(...), lesson_id: int = Form(...),
    weekday: str = Form(...), period: str = Form(...), teacher_id: str = Form(""),
    room: str = Form(""), user: dict = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    if not user or user["role"] != "teacher":
        raise HTTPException(status_code=403)
    day, period_num = _parse_int(weekday, -1, 0, 6), _parse_int(period, -1, 1, 8)
    if day < 0 or period_num < 0:
        return RedirectResponse("/teacher/timetable?error=invalid_slot", status_code=status.HTTP_302_FOUND)
    try:
        db.execute(
            """INSERT INTO lesson_timetables
               (term_id, lesson_id, grade_id, teacher_id, weekday, period, room, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (term_id, lesson_id, grade_id, _parse_int(teacher_id, 0) or None,
             day, period_num, (room or "").strip(), int(time.time())),
        )
        db.commit()
    except sqlite3.IntegrityError:
        return RedirectResponse("/teacher/timetable?error=slot_taken", status_code=status.HTTP_302_FOUND)
    return RedirectResponse(
        f"/teacher/timetable?term_id={term_id}&grade_id={grade_id}&created=1",
        status_code=status.HTTP_302_FOUND,
    )


@app.post("/teacher/timetable/{slot_id}/edit")
def teacher_timetable_edit(
    slot_id: int, term_id: int = Form(...), grade_id: int = Form(...), lesson_id: int = Form(...),
    weekday: str = Form(...), period: str = Form(...), teacher_id: str = Form(""),
    room: str = Form(""), user: dict = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    if not user or user["role"] != "teacher":
        raise HTTPException(status_code=403)
    day, period_num = _parse_int(weekday, -1, 0, 6), _parse_int(period, -1, 1, 8)
    if day < 0 or period_num < 0:
        return RedirectResponse(
            f"/teacher/timetable?term_id={term_id}&grade_id={grade_id}&error=invalid_slot",
            status_code=status.HTTP_302_FOUND,
        )
    try:
        db.execute(
            """UPDATE lesson_timetables SET lesson_id = ?, teacher_id = ?, weekday = ?, period = ?, room = ?
               WHERE id = ?""",
            (lesson_id, _parse_int(teacher_id, 0) or None, day, period_num, (room or "").strip(), slot_id),
        )
        db.commit()
    except sqlite3.IntegrityError:
        return RedirectResponse(
            f"/teacher/timetable?term_id={term_id}&grade_id={grade_id}&error=slot_taken",
            status_code=status.HTTP_302_FOUND,
        )
    return RedirectResponse(
        f"/teacher/timetable?term_id={term_id}&grade_id={grade_id}&updated=1",
        status_code=status.HTTP_302_FOUND,
    )


@app.post("/teacher/timetable/{slot_id}/delete")
def teacher_timetable_delete(slot_id: int, term_id: int = Form(...), grade_id: int = Form(...),
                             user: dict = Depends(get_current_user), db: sqlite3.Connection = Depends(get_db)):
    if not user or user["role"] != "teacher":
        raise HTTPException(status_code=403)
    db.execute("DELETE FROM lesson_timetables WHERE id = ?", (slot_id,))
    db.commit()
    return RedirectResponse(f"/teacher/timetable?term_id={term_id}&grade_id={grade_id}&deleted=1",
                            status_code=status.HTTP_302_FOUND)


@app.post("/teacher/timetable/generate")
def teacher_timetable_generate(term_id: int = Form(...), user: dict = Depends(get_current_user),
                               db: sqlite3.Connection = Depends(get_db)):
    if not user or user["role"] != "teacher":
        raise HTTPException(status_code=403)
    created = _generate_sessions(db, term_id, user["id"])
    return RedirectResponse(f"/teacher/sessions?term_id={term_id}&generated={created}",
                            status_code=status.HTTP_302_FOUND)


# --- ULIRLYN IRTS TOIRGOO, HICHEELIIN ODNUUD, SHALGUUR A-D ---

@app.get("/teacher/setup/attendance-config", response_class=HTMLResponse)
def teacher_attendance_config_page(
    request: Request, term_id: Optional[int] = None, grade_id: Optional[int] = None,
    user: dict = Depends(get_current_user), db: sqlite3.Connection = Depends(get_db),
):
    if not user or user["role"] != "teacher":
        return RedirectResponse("/login")
    terms, grades = _all_terms(db), _all_grades(db)
    selected_term = term_id or next((t["id"] for t in terms if t["is_active"]), terms[0]["id"] if terms else 0)
    selected_grade = grade_id or next((g["id"] for g in grades if g["is_active"]), grades[0]["id"] if grades else 0)
    configs = []
    if selected_term and selected_grade:
        for row in db.execute(
            """SELECT ac.*, l.title as lesson_title FROM term_attendance_config ac
               LEFT JOIN lessons l ON l.id = ac.lesson_id
               WHERE ac.term_id = ? AND ac.grade_id = ? ORDER BY ac.lesson_id""",
            (selected_term, selected_grade),
        ).fetchall():
            item = dict(row)
            item.update(_attendance_setting(db, selected_term, selected_grade, row["lesson_id"]))
            configs.append(item)
    preview = _attendance_setting(db, selected_term, selected_grade)
    lessons = [dict(r) for r in db.execute(
        """SELECT DISTINCT l.id, l.title FROM lessons l
           JOIN lesson_grades lg ON lg.lesson_id = l.id WHERE lg.grade_id = ? ORDER BY l.title""",
        (selected_grade,),
    ).fetchall()]
    return templates.TemplateResponse(
        request=request, name="teacher_attendance_config.html",
        context={
            "user": user, "terms": terms, "grades": grades, "lessons": lessons,
            "selected_term": selected_term, "selected_grade": selected_grade,
            "configs": configs, "preview": preview,
        },
    )


@app.post("/teacher/setup/attendance-config")
def teacher_attendance_config_save(
    term_id: int = Form(...), grade_id: int = Form(...), lesson_id: str = Form("0"),
    total_points: str = Form("30"), atl_skill: str = Form("self-management"),
    user: dict = Depends(get_current_user), db: sqlite3.Connection = Depends(get_db),
):
    if not user or user["role"] != "teacher":
        raise HTTPException(status_code=403)
    db.execute(
        """INSERT INTO term_attendance_config (term_id, grade_id, lesson_id, total_points, atl_skill, updated_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(term_id, grade_id, lesson_id)
           DO UPDATE SET total_points = excluded.total_points, atl_skill = excluded.atl_skill,
                         updated_at = excluded.updated_at""",
        (term_id, grade_id, _parse_int(lesson_id, 0, 0), _parse_int(total_points, 30, 1, 1000),
         (atl_skill or "self-management").strip() or "self-management", int(time.time())),
    )
    db.commit()
    return RedirectResponse(
        f"/teacher/setup/attendance-config?term_id={term_id}&grade_id={grade_id}&saved=1",
        status_code=status.HTTP_302_FOUND,
    )


@app.post("/teacher/setup/attendance-config/{config_id}/edit")
def teacher_attendance_config_edit(
    config_id: int, term_id: int = Form(...), grade_id: int = Form(...),
    lesson_id: str = Form("0"), total_points: str = Form("30"),
    atl_skill: str = Form("self-management"), user: dict = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    if not user or user["role"] != "teacher":
        raise HTTPException(status_code=403)
    try:
        db.execute(
            """UPDATE term_attendance_config SET lesson_id = ?, total_points = ?, atl_skill = ?, updated_at = ?
               WHERE id = ?""",
            (_parse_int(lesson_id, 0, 0), _parse_int(total_points, 30, 1, 1000),
             (atl_skill or "self-management").strip() or "self-management", int(time.time()), config_id),
        )
        db.commit()
    except sqlite3.IntegrityError:
        return RedirectResponse(
            f"/teacher/setup/attendance-config?term_id={term_id}&grade_id={grade_id}&error=exists",
            status_code=status.HTTP_302_FOUND,
        )
    return RedirectResponse(
        f"/teacher/setup/attendance-config?term_id={term_id}&grade_id={grade_id}&updated=1",
        status_code=status.HTTP_302_FOUND,
    )


@app.post("/teacher/setup/attendance-config/{config_id}/delete")
def teacher_attendance_config_delete(config_id: int, term_id: int = Form(...),
                                     grade_id: int = Form(...), user: dict = Depends(get_current_user),
                                     db: sqlite3.Connection = Depends(get_db)):
    if not user or user["role"] != "teacher":
        raise HTTPException(status_code=403)
    db.execute("DELETE FROM term_attendance_config WHERE id = ?", (config_id,))
    db.commit()
    return RedirectResponse(
        f"/teacher/setup/attendance-config?term_id={term_id}&grade_id={grade_id}&deleted=1",
        status_code=status.HTTP_302_FOUND,
    )


@app.get("/teacher/sessions", response_class=HTMLResponse)
def teacher_sessions_page(
    request: Request, term_id: Optional[int] = None, lesson_id: Optional[int] = None,
    grade_id: Optional[int] = None, user: dict = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    if not user or user["role"] != "teacher":
        return RedirectResponse("/login")
    terms = _all_terms(db)
    selected_term = term_id or next((t["id"] for t in terms if t["is_active"]), terms[0]["id"] if terms else 0)
    where, params = "s.term_id = ?", [selected_term]
    if lesson_id:
        where += " AND s.lesson_id = ?"
        params.append(lesson_id)
    if grade_id:
        where += " AND s.grade_id = ?"
        params.append(grade_id)
    sessions = [dict(r) for r in db.execute(
        f"""SELECT s.*, l.title as lesson_title, g.name as grade_name, t.name as term_name
            FROM lesson_sessions s JOIN lessons l ON l.id = s.lesson_id
            JOIN grades g ON g.id = s.grade_id JOIN academic_terms t ON t.id = s.term_id
            WHERE {where} ORDER BY s.date DESC""",
        params,
    ).fetchall()]
    return templates.TemplateResponse(
        request=request, name="teacher_sessions.html",
        context={
            "user": user, "terms": terms, "sessions": sessions,
            "lessons": [dict(r) for r in db.execute("SELECT id, title FROM lessons ORDER BY title").fetchall()],
            "grades": _all_grades(db), "selected_term": selected_term,
            "selected_lesson": lesson_id or 0, "selected_grade": grade_id or 0,
        },
    )


@app.post("/teacher/sessions")
def teacher_session_create(
    term_id: int = Form(...), lesson_id: int = Form(...), grade_id: int = Form(...),
    date: str = Form(...), topic: str = Form(""), user: dict = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    if not user or user["role"] != "teacher":
        raise HTTPException(status_code=403)
    day = _parse_date(date)
    if not day:
        return RedirectResponse(f"/teacher/sessions?term_id={term_id}&error=invalid_date",
                                status_code=status.HTTP_302_FOUND)
    try:
        db.execute(
            """INSERT INTO lesson_sessions (term_id, lesson_id, grade_id, date, topic, created_by, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (term_id, lesson_id, grade_id, day.isoformat(), (topic or "").strip(),
             user["id"], int(time.time())),
        )
        db.commit()
    except sqlite3.IntegrityError:
        return RedirectResponse(f"/teacher/sessions?term_id={term_id}&error=exists",
                                status_code=status.HTTP_302_FOUND)
    return RedirectResponse(f"/teacher/sessions?term_id={term_id}&created=1",
                            status_code=status.HTTP_302_FOUND)


@app.post("/teacher/sessions/{session_id}/delete")
def teacher_session_delete(session_id: int, term_id: int = Form(...),
                           user: dict = Depends(get_current_user), db: sqlite3.Connection = Depends(get_db)):
    if not user or user["role"] != "teacher":
        raise HTTPException(status_code=403)
    for sql in (
        "DELETE FROM criteria_scores WHERE session_id = ?",
        "DELETE FROM attendance_marks WHERE session_id = ?",
        "DELETE FROM lesson_conduct_records WHERE session_id = ?",
        "DELETE FROM lesson_sessions WHERE id = ?",
    ):
        db.execute(sql, (session_id,))
    db.commit()
    return RedirectResponse(f"/teacher/sessions?term_id={term_id}&deleted=1",
                            status_code=status.HTTP_302_FOUND)


@app.post("/teacher/sessions/{session_id}/edit")
def teacher_session_edit(
    session_id: int, term_id: int = Form(...), lesson_id: int = Form(...), grade_id: int = Form(...),
    date: str = Form(...), topic: str = Form(""), user: dict = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    if not user or user["role"] != "teacher":
        raise HTTPException(status_code=403)
    day = _parse_date(date)
    if not day:
        return RedirectResponse(f"/teacher/sessions?term_id={term_id}&error=invalid_date",
                                status_code=status.HTTP_302_FOUND)
    try:
        db.execute(
            """UPDATE lesson_sessions SET term_id = ?, lesson_id = ?, grade_id = ?, date = ?, topic = ?
               WHERE id = ?""",
            (term_id, lesson_id, grade_id, day.isoformat(), (topic or "").strip(), session_id),
        )
        db.commit()
    except sqlite3.IntegrityError:
        return RedirectResponse(f"/teacher/sessions?term_id={term_id}&error=exists",
                                status_code=status.HTTP_302_FOUND)
    return RedirectResponse(f"/teacher/sessions?term_id={term_id}&updated=1",
                            status_code=status.HTTP_302_FOUND)


@app.get("/teacher/sessions/{session_id}", response_class=HTMLResponse)
def teacher_session_grading_page(session_id: int, request: Request,
                                 user: dict = Depends(get_current_user),
                                 db: sqlite3.Connection = Depends(get_db)):
    if not user or user["role"] != "teacher":
        return RedirectResponse("/login")
    session = db.execute(
        """SELECT s.*, l.title as lesson_title, g.name as grade_name, t.name as term_name
           FROM lesson_sessions s JOIN lessons l ON l.id = s.lesson_id
           JOIN grades g ON g.id = s.grade_id JOIN academic_terms t ON t.id = s.term_id
           WHERE s.id = ?""",
        (session_id,),
    ).fetchone()
    if not session:
        raise HTTPException(status_code=404)
    setting = _attendance_setting(db, session["term_id"], session["grade_id"], session["lesson_id"])
    students = _students_for_grade_name(db, session["grade_name"])
    marks = {r["student_id"]: dict(r) for r in db.execute(
        "SELECT * FROM attendance_marks WHERE session_id = ?", (session_id,)
    ).fetchall()}
    criteria = {(r["student_id"], r["criterion"]): r["score"] for r in db.execute(
        "SELECT * FROM criteria_scores WHERE session_id = ?", (session_id,)
    ).fetchall()}
    conduct = {}
    for r in db.execute(
        "SELECT student_id, SUM(points) as total FROM lesson_conduct_records WHERE session_id = ? GROUP BY student_id",
        (session_id,),
    ).fetchall():
        conduct[r["student_id"]] = r["total"]
    rows = []
    for student in students:
        row = dict(student)
        row["mark"] = marks.get(student["id"], {})
        row["criteria"] = {letter: criteria.get((student["id"], letter), "") for letter in CRITERION_LETTERS}
        row["conduct"] = conduct.get(student["id"], 0)
        rows.append(row)
    return templates.TemplateResponse(
        request=request, name="teacher_session_grading.html",
        context={
            "user": user, "session": dict(session), "students": rows,
            "criteria": CRITERION_LETTERS, "setting": setting,
            "unit_plans": [dict(r) for r in db.execute(
                "SELECT id, title, unit_code FROM ib_unit_plans WHERE lesson_id = ? ORDER BY id",
                (session["lesson_id"],),
            ).fetchall()],
        },
    )


@app.post("/teacher/sessions/{session_id}")
async def teacher_session_grading_save(session_id: int, request: Request,
                                       user: dict = Depends(get_current_user),
                                       db: sqlite3.Connection = Depends(get_db)):
    if not user or user["role"] != "teacher":
        raise HTTPException(status_code=403)
    session = db.execute("SELECT * FROM lesson_sessions WHERE id = ?", (session_id,)).fetchone()
    if not session:
        raise HTTPException(status_code=404)
    form = await request.form()
    unit_plan_id = _parse_int(form.get("unit_plan_id"), 0) or None
    students = _students_for_grade_name(db, _grade_name(db, session["grade_id"]))
    now = int(time.time())
    for student in students:
        sid = student["id"]
        attendance_status = str(form.get(f"status_{sid}") or "present")
        if attendance_status not in {"present", "late", "absent"}:
            attendance_status = "present"
        points = _round1(form.get(f"points_{sid}"))
        db.execute(
            """INSERT INTO attendance_marks (session_id, student_id, status, points, note)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(session_id, student_id)
               DO UPDATE SET status = excluded.status, points = excluded.points, note = excluded.note""",
            (session_id, sid, attendance_status, points, str(form.get(f"note_{sid}") or "").strip()),
        )
        for letter in CRITERION_LETTERS:
            raw = form.get(f"c_{letter}_{sid}")
            if raw is None or str(raw).strip() == "":
                continue
            score = max(0.0, min(2.0, _round1(raw)))
            db.execute(
                """INSERT INTO criteria_scores
                   (session_id, student_id, unit_plan_id, criterion, score, updated_by, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(session_id, student_id, criterion)
                   DO UPDATE SET score = excluded.score, unit_plan_id = excluded.unit_plan_id,
                                 updated_by = excluded.updated_by, updated_at = excluded.updated_at""",
                (session_id, sid, unit_plan_id, letter, score, user["id"], now),
            )
        conduct = _round1(form.get(f"conduct_{sid}"))
        db.execute("DELETE FROM lesson_conduct_records WHERE session_id = ? AND student_id = ?",
                   (session_id, sid))
        if conduct:
            db.execute(
                """INSERT INTO lesson_conduct_records (session_id, student_id, points, note, created_by, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (session_id, sid, min(0.0, conduct), str(form.get(f"note_{sid}") or "").strip(),
                 user["id"], now),
            )
    db.commit()
    return RedirectResponse(f"/teacher/sessions/{session_id}?saved=1", status_code=status.HTTP_302_FOUND)


# --- DATA ANALYSIS: AI SHALGALT ---

def _grade_da_in_background(submission_id: int):
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """SELECT ds.*, a.title, a.description, a.max_score, a.csv_path, a.provider, a.model,
                      u.gemini_api_key, u.deepseek_api_key
               FROM data_analysis_submissions ds
               JOIN data_analysis_assignments a ON a.id = ds.assignment_id
               LEFT JOIN users u ON u.id = a.created_by
               WHERE ds.id = ?""",
            (submission_id,),
        ).fetchone()
        if not row:
            return
        provider = (row["provider"] or "gemini").strip()
        api_key = row["gemini_api_key"] if provider == "gemini" else row["deepseek_api_key"]
        csv_sample = ""
        if row["csv_path"]:
            csv_full = os.path.join(UPLOAD_CSV, row["csv_path"])
            if os.path.exists(csv_full):
                with open(csv_full, "r", encoding="utf-8", errors="ignore") as handle:
                    csv_sample = handle.read()[:6000]
        result = grade_document(
            {
                "title": row["title"], "description": row["description"],
                "max_score": row["max_score"],
            },
            os.path.join(UPLOAD_DOCX, row["docx_path"]),
            provider, api_key, row["model"] or "", csv_sample,
        )
        conn.execute(
            """UPDATE data_analysis_submissions
               SET status = 'graded', score = ?, ib_score = ?, criteria_json = ?, feedback = ?,
                   graded_at = ?, error = NULL WHERE id = ?""",
            (result["score"], result["ib_total"],
             json.dumps(result["criteria"], ensure_ascii=False), result["feedback"],
             int(time.time()), submission_id),
        )
    except Exception as exc:
        conn.execute(
            "UPDATE data_analysis_submissions SET status = 'error', error = ? WHERE id = ?",
            (f"AI шалгах үед алдаа гарлаа: {exc}", submission_id),
        )
    finally:
        conn.commit()
        conn.close()


@app.get("/teacher/data-analysis", response_class=HTMLResponse)
def teacher_data_analysis_page(
    request: Request, term_id: Optional[int] = None, grade_id: Optional[int] = None,
    user: dict = Depends(get_current_user), db: sqlite3.Connection = Depends(get_db),
):
    if not user or user["role"] != "teacher":
        return RedirectResponse("/login")
    terms, grades = _all_terms(db), _all_grades(db)
    selected_term = term_id or next((t["id"] for t in terms if t["is_active"]), terms[0]["id"] if terms else 0)
    selected_grade = grade_id or next((g["id"] for g in grades if g["is_active"]), grades[0]["id"] if grades else 0)
    where, params = "a.term_id = ?", [selected_term]
    if selected_grade:
        where += " AND a.grade_id = ?"
        params.append(selected_grade)
    assignments = []
    for row in db.execute(
        f"""SELECT a.*, l.title as lesson_title, g.name as grade_name, t.name as term_name,
                   p.title as unit_title,
                   (SELECT COUNT(*) FROM data_analysis_submissions s WHERE s.assignment_id = a.id) as total_subs,
                   (SELECT COUNT(*) FROM data_analysis_submissions s
                    WHERE s.assignment_id = a.id AND s.status = 'graded') as graded_subs
            FROM data_analysis_assignments a
            LEFT JOIN lessons l ON l.id = a.lesson_id JOIN grades g ON g.id = a.grade_id
            JOIN academic_terms t ON t.id = a.term_id
            LEFT JOIN ib_unit_plans p ON p.id = a.unit_plan_id
            WHERE {where} ORDER BY a.id DESC""",
        params,
    ).fetchall():
        assignments.append(dict(row))
    lessons = [dict(r) for r in db.execute(
        """SELECT DISTINCT l.id, l.title FROM lessons l
           JOIN lesson_grades lg ON lg.lesson_id = l.id WHERE lg.grade_id = ? ORDER BY l.title""",
        (selected_grade,),
    ).fetchall()]
    return templates.TemplateResponse(
        request=request, name="teacher_data_analysis.html",
        context={
            "user": user, "terms": terms, "grades": grades, "lessons": lessons,
            "assignments": assignments, "selected_term": selected_term,
            "selected_grade": selected_grade,
            "unit_plans": [dict(r) for r in db.execute(
                "SELECT id, title, lesson_id FROM ib_unit_plans ORDER BY id DESC"
            ).fetchall()],
        },
    )


@app.post("/teacher/data-analysis")
async def teacher_data_analysis_create(request: Request, user: dict = Depends(get_current_user),
                                       db: sqlite3.Connection = Depends(get_db)):
    if not user or user["role"] != "teacher":
        raise HTTPException(status_code=403)
    form = await request.form()
    title = str(form.get("title") or "").strip()
    if not title:
        return RedirectResponse("/teacher/data-analysis?error=missing_title", status_code=status.HTTP_302_FOUND)
    csv_path = None
    csv_file = form.get("csv_file")
    if _is_upload(csv_file) and csv_file.filename:
        csv_path = _safe_upload_name(csv_file.filename, f"csv_{user['id']}", {".csv", ".txt"})
        if not csv_path:
            return RedirectResponse("/teacher/data-analysis?error=invalid_csv", status_code=status.HTTP_302_FOUND)
        with open(os.path.join(UPLOAD_CSV, csv_path), "wb") as buf:
            shutil.copyfileobj(csv_file.file, buf)
    term_id = _parse_int(form.get("term_id"), 0)
    grade_id = _parse_int(form.get("grade_id"), 0)
    lesson_id = _parse_int(form.get("lesson_id"), 0)
    if not term_id or not grade_id or not lesson_id:
        return RedirectResponse("/teacher/data-analysis?error=missing_selection", status_code=status.HTTP_302_FOUND)
    if not db.execute("SELECT 1 FROM academic_terms WHERE id = ?", (term_id,)).fetchone():
        return RedirectResponse("/teacher/data-analysis?error=invalid_selection", status_code=status.HTTP_302_FOUND)
    if not db.execute("SELECT 1 FROM grades WHERE id = ?", (grade_id,)).fetchone():
        return RedirectResponse("/teacher/data-analysis?error=invalid_selection", status_code=status.HTTP_302_FOUND)
    if not db.execute("SELECT 1 FROM lessons WHERE id = ?", (lesson_id,)).fetchone():
        return RedirectResponse("/teacher/data-analysis?error=invalid_selection", status_code=status.HTTP_302_FOUND)
    db.execute(
        """INSERT INTO data_analysis_assignments
           (term_id, lesson_id, grade_id, unit_plan_id, title, description, max_score, csv_path,
            due_date, provider, model, created_by, created_at, is_active)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
        (
            term_id, lesson_id, grade_id,
            _parse_int(form.get("unit_plan_id"), 0) or None,
            title, str(form.get("description") or "").strip(),
            _parse_int(form.get("max_score"), 20, 1, 1000), csv_path,
            _normalize_date(str(form.get("due_date") or "")),
            str(form.get("provider") or "gemini").strip(),
            str(form.get("model") or "").strip(), user["id"], int(time.time()),
        ),
    )
    db.commit()
    return RedirectResponse("/teacher/data-analysis?created=1", status_code=status.HTTP_302_FOUND)


@app.post("/teacher/data-analysis/{assignment_id}/edit")
async def teacher_data_analysis_edit(assignment_id: int, request: Request,
                                     user: dict = Depends(get_current_user),
                                     db: sqlite3.Connection = Depends(get_db)):
    if not user or user["role"] != "teacher":
        raise HTTPException(status_code=403)
    if not db.execute("SELECT 1 FROM data_analysis_assignments WHERE id = ?", (assignment_id,)).fetchone():
        raise HTTPException(status_code=404)
    form = await request.form()
    title = str(form.get("title") or "").strip()
    if not title:
        return RedirectResponse("/teacher/data-analysis?error=missing_title", status_code=status.HTTP_302_FOUND)
    csv_path = None
    csv_file = form.get("csv_file")
    if _is_upload(csv_file) and csv_file.filename:
        csv_path = _safe_upload_name(csv_file.filename, f"csv_{user['id']}", {".csv", ".txt"})
        if not csv_path:
            return RedirectResponse("/teacher/data-analysis?error=invalid_csv", status_code=status.HTTP_302_FOUND)
        with open(os.path.join(UPLOAD_CSV, csv_path), "wb") as buf:
            shutil.copyfileobj(csv_file.file, buf)
    sql = """UPDATE data_analysis_assignments
             SET term_id = ?, lesson_id = ?, grade_id = ?, unit_plan_id = ?, title = ?, description = ?,
                 max_score = ?, due_date = ?, provider = ?, model = ?"""
    params = [
        _parse_int(form.get("term_id"), 0), _parse_int(form.get("lesson_id"), 0),
        _parse_int(form.get("grade_id"), 0), _parse_int(form.get("unit_plan_id"), 0) or None,
        title, str(form.get("description") or "").strip(),
        _parse_int(form.get("max_score"), 20, 1, 1000),
        _normalize_date(str(form.get("due_date") or "")),
        str(form.get("provider") or "gemini").strip(), str(form.get("model") or "").strip(),
    ]
    if csv_path:
        sql += ", csv_path = ?"
        params.append(csv_path)
    sql += " WHERE id = ?"
    params.append(assignment_id)
    db.execute(sql, params)
    db.commit()
    return RedirectResponse("/teacher/data-analysis?updated=1", status_code=status.HTTP_302_FOUND)


@app.get("/teacher/data-analysis/{assignment_id}", response_class=HTMLResponse)
def teacher_data_analysis_detail(assignment_id: int, request: Request,
                                 user: dict = Depends(get_current_user),
                                 db: sqlite3.Connection = Depends(get_db)):
    if not user or user["role"] != "teacher":
        return RedirectResponse("/login")
    assignment = db.execute(
        """SELECT a.*, l.title as lesson_title, g.name as grade_name, t.name as term_name,
                  p.title as unit_title
           FROM data_analysis_assignments a JOIN lessons l ON l.id = a.lesson_id
           JOIN grades g ON g.id = a.grade_id JOIN academic_terms t ON t.id = a.term_id
           LEFT JOIN ib_unit_plans p ON p.id = a.unit_plan_id WHERE a.id = ?""",
        (assignment_id,),
    ).fetchone()
    if not assignment:
        raise HTTPException(status_code=404)
    submissions = []
    for row in db.execute(
        """SELECT s.*, u.name, u.surname, u.username, u.student_code, u.photo_path
           FROM data_analysis_submissions s JOIN users u ON u.id = s.student_id
           WHERE s.assignment_id = ? ORDER BY u.surname, u.name""",
        (assignment_id,),
    ).fetchall():
        item = dict(row)
        try:
            item["criteria"] = json.loads(item["criteria_json"]) if item["criteria_json"] else {}
        except (TypeError, ValueError):
            item["criteria"] = {}
        submissions.append(item)
    return templates.TemplateResponse(
        request=request, name="teacher_data_analysis_detail.html",
        context={"user": user, "assignment": dict(assignment), "submissions": submissions},
    )


@app.post("/teacher/data-analysis/{assignment_id}/grade-all")
def teacher_data_analysis_grade_all(assignment_id: int, user: dict = Depends(get_current_user),
                                    db: sqlite3.Connection = Depends(get_db)):
    if not user or user["role"] != "teacher":
        raise HTTPException(status_code=403)
    pending = [row[0] for row in db.execute(
        """SELECT id FROM data_analysis_submissions
           WHERE assignment_id = ? AND status IN ('pending', 'error')""",
        (assignment_id,),
    ).fetchall()]
    for submission_id in pending:
        db.execute("UPDATE data_analysis_submissions SET status = 'checking' WHERE id = ?", (submission_id,))
        threading.Thread(target=_grade_da_in_background, args=(submission_id,), daemon=True).start()
    db.commit()
    return RedirectResponse(f"/teacher/data-analysis/{assignment_id}?grading={len(pending)}",
                            status_code=status.HTTP_302_FOUND)


@app.post("/teacher/data-analysis/{assignment_id}/submission/{submission_id}/grade")
def teacher_data_analysis_manual_grade(
    assignment_id: int, submission_id: int, score: str = Form(...), feedback: str = Form(""),
    user: dict = Depends(get_current_user), db: sqlite3.Connection = Depends(get_db),
):
    if not user or user["role"] != "teacher":
        raise HTTPException(status_code=403)
    assignment = db.execute(
        "SELECT max_score FROM data_analysis_assignments WHERE id = ?", (assignment_id,)
    ).fetchone()
    if not assignment:
        raise HTTPException(status_code=404)
    max_score = max(1, int(assignment["max_score"] or 20))
    value = max(0.0, min(float(max_score), _round1(score)))
    db.execute(
        """UPDATE data_analysis_submissions
           SET status = 'graded', score = ?, ib_score = ?, feedback = ?, graded_at = ?, error = NULL
           WHERE id = ? AND assignment_id = ?""",
        (value, round(value / max_score * 8, 1), (feedback or "").strip(),
         int(time.time()), submission_id, assignment_id),
    )
    db.commit()
    return RedirectResponse(f"/teacher/data-analysis/{assignment_id}?saved=1",
                            status_code=status.HTTP_302_FOUND)


@app.get("/data-analysis/csv/{filename}")
def get_data_analysis_csv(filename: str, user: dict = Depends(get_current_user)):
    if not user:
        raise HTTPException(status_code=403)
    safe = os.path.basename(filename)
    return FileResponse(os.path.join(UPLOAD_CSV, safe), media_type="text/csv", filename=safe)


@app.get("/student/data-analysis", response_class=HTMLResponse)
def student_data_analysis_page(request: Request, user: dict = Depends(get_current_user),
                               db: sqlite3.Connection = Depends(get_db)):
    if not user or user["role"] != "student":
        return RedirectResponse("/login")
    grade_name = (user["grade"] or "").strip().lower()
    rows = db.execute(
        """SELECT a.*, l.title as lesson_title, t.name as term_name, p.title as unit_title,
                  s.status as submission_status, s.score, s.ib_score, s.id as submission_id
           FROM data_analysis_assignments a
           JOIN lessons l ON l.id = a.lesson_id
           JOIN academic_terms t ON t.id = a.term_id
           JOIN grades g ON g.id = a.grade_id
           LEFT JOIN ib_unit_plans p ON p.id = a.unit_plan_id
           LEFT JOIN data_analysis_submissions s
                  ON s.assignment_id = a.id AND s.student_id = ?
           WHERE lower(g.name) = ? AND a.is_active = 1
           ORDER BY a.id DESC""",
        (user["id"], grade_name),
    ).fetchall()
    return templates.TemplateResponse(
        request=request, name="student_data_analysis.html",
        context={"user": user, "assignments": [dict(r) for r in rows]},
    )


@app.get("/student/data-analysis/{assignment_id}", response_class=HTMLResponse)
def student_data_analysis_detail(assignment_id: int, request: Request,
                                 user: dict = Depends(get_current_user),
                                 db: sqlite3.Connection = Depends(get_db)):
    if not user or user["role"] != "student":
        return RedirectResponse("/login")
    assignment = db.execute(
        """SELECT a.*, l.title as lesson_title, t.name as term_name, g.name as grade_name,
                  p.title as unit_title
           FROM data_analysis_assignments a JOIN lessons l ON l.id = a.lesson_id
           JOIN grades g ON g.id = a.grade_id JOIN academic_terms t ON t.id = a.term_id
           LEFT JOIN ib_unit_plans p ON p.id = a.unit_plan_id WHERE a.id = ?""",
        (assignment_id,),
    ).fetchone()
    if not assignment or (assignment["grade_name"] or "").lower() != (user["grade"] or "").strip().lower():
        raise HTTPException(status_code=404)
    submission = db.execute(
        "SELECT * FROM data_analysis_submissions WHERE assignment_id = ? AND student_id = ?",
        (assignment_id, user["id"]),
    ).fetchone()
    criteria = {}
    if submission and submission["criteria_json"]:
        try:
            criteria = json.loads(submission["criteria_json"])
        except (TypeError, ValueError):
            criteria = {}
    return templates.TemplateResponse(
        request=request, name="student_data_analysis_detail.html",
        context={
            "user": user, "assignment": dict(assignment),
            "submission": dict(submission) if submission else None,
            "criteria": criteria,
        },
    )


@app.post("/student/data-analysis/{assignment_id}/submit")
async def student_data_analysis_submit(assignment_id: int, request: Request,
                                       user: dict = Depends(get_current_user),
                                       db: sqlite3.Connection = Depends(get_db)):
    if not user or user["role"] != "student":
        raise HTTPException(status_code=403)
    assignment = db.execute(
        """SELECT a.*, g.name as grade_name FROM data_analysis_assignments a
           JOIN grades g ON g.id = a.grade_id WHERE a.id = ?""",
        (assignment_id,),
    ).fetchone()
    if not assignment or (assignment["grade_name"] or "").lower() != (user["grade"] or "").strip().lower():
        raise HTTPException(status_code=404)
    form = await request.form()
    docx_file = form.get("docx_file")
    if not _is_upload(docx_file) or not docx_file.filename:
        return RedirectResponse(f"/student/data-analysis/{assignment_id}?error=file_required",
                                status_code=status.HTTP_302_FOUND)
    safe_name = _safe_upload_name(docx_file.filename, f"da_{user['id']}", {".docx"})
    if not safe_name:
        return RedirectResponse(f"/student/data-analysis/{assignment_id}?error=invalid_file",
                                status_code=status.HTTP_302_FOUND)
    with open(os.path.join(UPLOAD_DOCX, safe_name), "wb") as buf:
        shutil.copyfileobj(docx_file.file, buf)
    now = int(time.time())
    existing = db.execute(
        "SELECT id, docx_path FROM data_analysis_submissions WHERE assignment_id = ? AND student_id = ?",
        (assignment_id, user["id"]),
    ).fetchone()
    if existing:
        submission_id = existing["id"]
        old_path = os.path.join(UPLOAD_DOCX, existing["docx_path"])
        db.execute(
            """UPDATE data_analysis_submissions
               SET docx_path = ?, file_name = ?, status = 'checking', score = NULL, ib_score = NULL,
                   criteria_json = NULL, feedback = NULL, error = NULL, submitted_at = ?, graded_at = NULL
               WHERE id = ?""",
            (safe_name, docx_file.filename, now, submission_id),
        )
        if os.path.exists(old_path):
            try:
                os.remove(old_path)
            except OSError:
                pass
    else:
        db.execute(
            """INSERT INTO data_analysis_submissions
               (assignment_id, student_id, docx_path, file_name, status, submitted_at)
               VALUES (?, ?, ?, ?, 'checking', ?)""",
            (assignment_id, user["id"], safe_name, docx_file.filename, now),
        )
        submission_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.commit()
    threading.Thread(target=_grade_da_in_background, args=(submission_id,), daemon=True).start()
    return RedirectResponse(f"/student/data-analysis/{assignment_id}?submitted=1",
                            status_code=status.HTTP_302_FOUND)


# --- ULIRLYN DUN, BASHI ZASAH ---

@app.get("/teacher/term-grades", response_class=HTMLResponse)
def teacher_term_grades_page(
    request: Request, term_id: Optional[int] = None, grade_id: Optional[int] = None,
    lesson_id: Optional[int] = None, user: dict = Depends(get_current_user),
    db: sqlite3.Connection = Depends(get_db),
):
    if not user or user["role"] != "teacher":
        return RedirectResponse("/login")
    terms, grades = _all_terms(db), _all_grades(db)
    selected_term = term_id or next((t["id"] for t in terms if t["is_active"]), terms[0]["id"] if terms else 0)
    selected_grade = grade_id or next((g["id"] for g in grades if g["is_active"]), grades[0]["id"] if grades else 0)
    lessons = [dict(r) for r in db.execute(
        """SELECT DISTINCT l.id, l.title FROM lessons l
           JOIN lesson_grades lg ON lg.lesson_id = l.id WHERE lg.grade_id = ? ORDER BY l.title""",
        (selected_grade,),
    ).fetchall()]
    selected_lesson = lesson_id or (lessons[0]["id"] if lessons else 0)
    grade_name = _grade_name(db, selected_grade)
    rows = []
    if selected_lesson and selected_term:
        for student in _students_for_grade_name(db, grade_name):
            item = dict(student)
            item.update(_term_grade_breakdown(db, selected_term, student["id"], selected_lesson, selected_grade))
            rows.append(item)
    return templates.TemplateResponse(
        request=request, name="teacher_term_grades.html",
        context={
            "user": user, "terms": terms, "grades": grades, "lessons": lessons,
            "selected_term": selected_term, "selected_grade": selected_grade,
            "selected_lesson": selected_lesson, "rows": rows, "weights": TERM_WEIGHTS,
        },
    )


@app.post("/teacher/term-grades")
async def teacher_term_grades_save(request: Request, user: dict = Depends(get_current_user),
                                   db: sqlite3.Connection = Depends(get_db)):
    if not user or user["role"] != "teacher":
        raise HTTPException(status_code=403)
    form = await request.form()
    term_id = _parse_int(form.get("term_id"), 0)
    lesson_id = _parse_int(form.get("lesson_id"), 0)
    grade_id = _parse_int(form.get("grade_id"), 0)
    saved = 0
    for key in form.keys():
        if not str(key).startswith("final_"):
            continue
        student_id = _parse_int(str(key).replace("final_", ""), 0)
        if not student_id:
            continue
        final_raw = str(form.get(key) or "").strip()
        if final_raw == "":
            db.execute(
                "DELETE FROM term_grade_overrides WHERE term_id = ? AND student_id = ? AND lesson_id = ?",
                (term_id, student_id, lesson_id),
            )
            continue
        final_100 = max(0.0, min(100.0, _round1(final_raw)))
        ib_raw = str(form.get(f"ib_{student_id}") or "").strip()
        ib_score = max(0.0, min(8.0, _round1(ib_raw))) if ib_raw else round(final_100 / 12.5, 1)
        db.execute(
            """INSERT INTO term_grade_overrides
               (term_id, student_id, lesson_id, final_100, ib_score, note, updated_by, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(term_id, student_id, lesson_id)
               DO UPDATE SET final_100 = excluded.final_100, ib_score = excluded.ib_score,
                             note = excluded.note, updated_by = excluded.updated_by,
                             updated_at = excluded.updated_at""",
            (term_id, student_id, lesson_id, final_100, ib_score,
             str(form.get(f"note_{student_id}") or "").strip(), user["id"], int(time.time())),
        )
        saved += 1
    db.commit()
    return RedirectResponse(
        f"/teacher/term-grades?term_id={term_id}&grade_id={grade_id}&lesson_id={lesson_id}&saved={saved}",
        status_code=status.HTTP_302_FOUND,
    )


@app.get("/student/term-grades", response_class=HTMLResponse)
def student_term_grades_page(request: Request, term_id: Optional[int] = None,
                             user: dict = Depends(get_current_user),
                             db: sqlite3.Connection = Depends(get_db)):
    if not user or user["role"] != "student":
        return RedirectResponse("/login")
    terms = _all_terms(db)
    selected_term = term_id or next((t["id"] for t in terms if t["is_active"]), terms[0]["id"] if terms else 0)
    grade_id = _grade_id_by_name(db, user["grade"] or "")
    rows = []
    for lesson in _visible_lessons_for_student(db, user["grade"] or ""):
        item = dict(lesson)
        item.update(_term_grade_breakdown(db, selected_term, user["id"], lesson["id"], grade_id or 0))
        rows.append(item)
    return templates.TemplateResponse(
        request=request, name="student_term_grades.html",
        context={"user": user, "terms": terms, "selected_term": selected_term, "rows": rows},
    )


# --- СУРАГЧИЙН ТАЙЛАН (HTML / PDF) ---

@app.get("/teacher/reports", response_class=HTMLResponse)
def teacher_reports_page(
    request: Request, grade: Optional[str] = None, term_id: Optional[int] = None,
    user: dict = Depends(get_current_user), db: sqlite3.Connection = Depends(get_db),
):
    if not user or user["role"] != "teacher":
        return RedirectResponse("/login")
    grades = _grades_with_students(db)
    terms = _all_terms(db)
    selected_grade = grade or (grades[0] if grades else "")
    selected_term = term_id or next(
        (t["id"] for t in terms if t["is_active"]), terms[0]["id"] if terms else 0
    )
    rows = []
    for student in _students_for_grade_name(db, selected_grade):
        report = _term_report(db, student["id"], selected_term)
        if report:
            rows.append(report)
    return templates.TemplateResponse(
        request=request, name="teacher_reports.html",
        context={
            "user": user, "grades": grades, "terms": terms,
            "selected_grade": selected_grade, "selected_term": selected_term, "rows": rows,
        },
    )


@app.get("/teacher/reports/{student_id}", response_class=HTMLResponse)
def teacher_report_page(
    student_id: int, request: Request, term_id: Optional[int] = None,
    user: dict = Depends(get_current_user), db: sqlite3.Connection = Depends(get_db),
):
    if not user or user["role"] != "teacher":
        return RedirectResponse("/login")
    report = _term_report(db, student_id, term_id)
    if not report:
        raise HTTPException(status_code=404, detail="Сурагч олдсонгүй")
    return templates.TemplateResponse(
        request=request, name="student_report.html",
        context={"user": user, "report": report, "own": False},
    )


@app.get("/teacher/reports/{student_id}/pdf")
def teacher_report_pdf(
    student_id: int, term_id: Optional[int] = None,
    user: dict = Depends(get_current_user), db: sqlite3.Connection = Depends(get_db),
):
    if not user or user["role"] != "teacher":
        raise HTTPException(status_code=403)
    report = _term_report(db, student_id, term_id)
    if not report:
        raise HTTPException(status_code=404, detail="Сурагч олдсонгүй")
    filename = re.sub(r"[^A-Za-z0-9_-]+", "_", report["student"].get("student_code") or str(student_id))
    return Response(
        content=_report_pdf(report), media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="report_{filename}.pdf"'},
    )


@app.get("/student/report", response_class=HTMLResponse)
def student_report_page(
    request: Request, term_id: Optional[int] = None,
    user: dict = Depends(get_current_user), db: sqlite3.Connection = Depends(get_db),
):
    if not user or user["role"] != "student":
        return RedirectResponse("/login")
    report = _term_report(db, user["id"], term_id)
    return templates.TemplateResponse(
        request=request, name="student_report.html",
        context={"user": user, "report": report, "own": True},
    )


@app.get("/student/report/pdf")
def student_report_pdf(
    term_id: Optional[int] = None,
    user: dict = Depends(get_current_user), db: sqlite3.Connection = Depends(get_db),
):
    if not user or user["role"] != "student":
        raise HTTPException(status_code=403)
    report = _term_report(db, user["id"], term_id)
    return Response(
        content=_report_pdf(report), media_type="application/pdf",
        headers={"Content-Disposition": "attachment; filename=\"my_report.pdf\""},
    )


# --- CSV EXPORT / IMPORT: SUragchid, dun ---

CSV_HEADERS = {
    "students": ["student_code", "username", "name", "surname", "grade", "email",
                 "gender", "phone", "instagram", "password"],
    "quiz_scores": ["student_code", "quiz_title", "total_score"],
    "term_grades": ["student_code", "term_name", "school_year", "lesson_title",
                    "final_100", "ib_score", "note"],
}

CSV_SAMPLES = {
    "students": [
        ["20260001", "20260001", "Болд", "Бат", "10a", "bold@example.com", "эрэгтэй",
         "99112233", "bold_ib", "student123"],
        ["20260002", "20260002", "Цэрэн", "Дорж", "10a", "tserэн@example.com", "эмэгтэй",
         "99887766", "", "student123"],
    ],
    "quiz_scores": [
        ["20260001", "Математик — 1-р шалгалт", "7"],
        ["20260002", "Математик — 1-р шалгалт", "6"],
    ],
    "term_grades": [
        ["20260001", "1-р улирал", "2026-2027", "Математик", "88", "7", "Сайн ажилласан"],
        ["20260002", "1-р улирал", "2026-2027", "Математик", "75", "6", ""],
    ],
}

CSV_LABELS = {
    "students": "Сурагчид",
    "quiz_scores": "Шалгалтын дүн",
    "term_grades": "Улирлын дүн",
}


def _transfer_page(request: Request, user: dict, db: sqlite3.Connection, result: dict | None = None,
                   error: str = ""):
    counts = {
        "students": db.execute("SELECT COUNT(*) FROM users WHERE role = 'student'").fetchone()[0],
        "quiz_scores": db.execute("SELECT COUNT(*) FROM submissions").fetchone()[0],
        "term_grades": db.execute("SELECT COUNT(*) FROM term_grade_overrides").fetchone()[0],
    }
    return templates.TemplateResponse(
        request=request, name="teacher_data_transfer.html",
        context={
            "user": user, "counts": counts, "labels": CSV_LABELS,
            "headers": CSV_HEADERS, "result": result, "error": error,
        },
    )


@app.get("/teacher/data-transfer", response_class=HTMLResponse)
def teacher_data_transfer_page(request: Request, user: dict = Depends(get_current_user),
                               db: sqlite3.Connection = Depends(get_db)):
    if not user or user["role"] != "teacher":
        return RedirectResponse("/login")
    return _transfer_page(request, user, db)


@app.get("/teacher/data-transfer/sample/{kind}")
def teacher_data_transfer_sample(kind: str, user: dict = Depends(get_current_user),
                                 db: sqlite3.Connection = Depends(get_db)):
    if not user or user["role"] != "teacher":
        raise HTTPException(status_code=403)
    if kind not in CSV_HEADERS:
        raise HTTPException(status_code=404)
    rows = [CSV_HEADERS[kind], *CSV_SAMPLES[kind]]
    return _csv_response(f"{kind}_sample.csv", rows)


@app.get("/teacher/data-transfer/export/{kind}")
def teacher_data_transfer_export(kind: str, user: dict = Depends(get_current_user),
                                 db: sqlite3.Connection = Depends(get_db)):
    if not user or user["role"] != "teacher":
        raise HTTPException(status_code=403)
    if kind not in CSV_HEADERS:
        raise HTTPException(status_code=404)
    rows = [CSV_HEADERS[kind]]
    if kind == "students":
        for r in db.execute(
            """SELECT student_code, username, name, surname, grade, email, gender, phone, instagram
               FROM users WHERE role = 'student' ORDER BY grade, surname, name, id"""
        ).fetchall():
            rows.append([r[key] or "" for key in CSV_HEADERS[kind][:-1]] + [""])
    elif kind == "quiz_scores":
        for r in db.execute(
            """SELECT u.student_code, q.title as quiz_title, s.total_score
               FROM submissions s JOIN quizzes q ON q.id = s.quiz_id
               JOIN users u ON u.id = s.student_id
               WHERE u.role = 'student' ORDER BY u.grade, u.surname, q.title"""
        ).fetchall():
            rows.append([r["student_code"] or "", r["quiz_title"] or "", r["total_score"]])
    else:
        for r in db.execute(
            """SELECT u.student_code, t.id as term_id, t.name as term_name, t.school_year,
                      l.title as lesson_title, o.student_id, o.lesson_id, o.note
               FROM term_grade_overrides o
               JOIN users u ON u.id = o.student_id
               JOIN academic_terms t ON t.id = o.term_id
               JOIN lessons l ON l.id = o.lesson_id
               ORDER BY t.start_date, u.grade, u.surname"""
        ).fetchall():
            breakdown = _term_grade_breakdown(
                db, r["term_id"], r["student_id"], r["lesson_id"], 0
            )
            rows.append([r["student_code"] or "", r["term_name"] or "", r["school_year"] or "",
                         r["lesson_title"] or "", breakdown["final_100"], breakdown["final_ib"],
                         r["note"] or ""])
    return _csv_response(f"{kind}.csv", rows)


@app.post("/teacher/data-transfer/import/{kind}")
async def teacher_data_transfer_import(kind: str, request: Request,
                                       file: Optional[UploadFile] = File(None),
                                       user: dict = Depends(get_current_user),
                                       db: sqlite3.Connection = Depends(get_db)):
    if not user or user["role"] != "teacher":
        raise HTTPException(status_code=403)
    if kind not in CSV_HEADERS:
        raise HTTPException(status_code=404)
    if not file or not file.filename:
        return _transfer_page(request, user, db, error="Файл сонгоно уу.")
    text = (await file.read()).decode("utf-8-sig", errors="replace")
    rows = _csv_dicts(text)
    if not rows:
        return _transfer_page(request, user, db, error="CSV файл хоосон байна.")

    imported, skipped = 0, 0
    errors: list[str] = []

    if kind == "students":
        for index, row in enumerate(rows, start=2):
            code = row.get("student_code", "").strip()
            username = row.get("username", "").strip() or code
            grade = row.get("grade", "").strip().lower()
            email = row.get("email", "").strip().lower()
            if not code or not username or not parse_grade(grade) or not email:
                errors.append(f"Мөр {index}: код, нэвтрэх нэр, анги эсвэл имэйл буруу.")
                skipped += 1
                continue
            password = (row.get("password") or "").strip()
            values = (
                username, password, grade,
                row.get("name", "").strip(), row.get("surname", "").strip(), code, email,
                row.get("gender", "").strip(), row.get("phone", "").strip(),
                row.get("instagram", "").strip() or None,
            )
            existing = db.execute(
                "SELECT id FROM users WHERE student_code = ? OR username = ?", (code, username)
            ).fetchone()
            try:
                if existing:
                    db.execute(
                        """UPDATE users SET username = ?, grade = ?, name = ?, surname = ?, student_code = ?,
                           email = ?, gender = ?, phone = ?, instagram = ?
                           WHERE id = ?""",
                        (values[0], *values[2:], existing["id"]),
                    )
                    if password:
                        db.execute("UPDATE users SET password = ? WHERE id = ?", (password, existing["id"]))
                else:
                    db.execute(
                        """INSERT INTO users
                           (username, password, role, grade, name, surname, student_code, email,
                            gender, phone, instagram)
                           VALUES (?, ?, 'student', ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (values[0], password or "student123", *values[2:]),
                    )
                imported += 1
            except sqlite3.IntegrityError as exc:
                errors.append(f"Мөр {index}: давхардсан өгөгдөл ({exc}).")
                skipped += 1

    elif kind == "quiz_scores":
        for index, row in enumerate(rows, start=2):
            code = row.get("student_code", "").strip()
            quiz_title = row.get("quiz_title", "").strip()
            raw_score = row.get("total_score", "").strip()
            if not code or not quiz_title or not raw_score:
                errors.append(f"Мөр {index}: код, шалгалт эсвэл оноо хоосон.")
                skipped += 1
                continue
            student = db.execute(
                "SELECT id FROM users WHERE role = 'student' AND (student_code = ? OR username = ?)",
                (code, code),
            ).fetchone()
            quiz = db.execute(
                "SELECT id FROM quizzes WHERE lower(title) = lower(?) ORDER BY id DESC LIMIT 1",
                (quiz_title,),
            ).fetchone()
            if not student or not quiz:
                errors.append(f"Мөр {index}: сурагч эсвэл шалгалт олдсонгүй.")
                skipped += 1
                continue
            score = _parse_int(raw_score, 0, 0, 8)
            existing = db.execute(
                "SELECT id FROM submissions WHERE quiz_id = ? AND student_id = ? ORDER BY id DESC LIMIT 1",
                (quiz["id"], student["id"]),
            ).fetchone()
            if existing:
                db.execute("UPDATE submissions SET total_score = ? WHERE id = ?", (score, existing["id"]))
            else:
                db.execute(
                    """INSERT INTO submissions (quiz_id, student_id, responses_json, evaluation_json, total_score)
                       VALUES (?, ?, '[]', '{}', ?)""",
                    (quiz["id"], student["id"], score),
                )
            imported += 1

    else:
        for index, row in enumerate(rows, start=2):
            code = row.get("student_code", "").strip()
            term_name = row.get("term_name", "").strip()
            lesson_title = row.get("lesson_title", "").strip()
            if not code or not term_name or not lesson_title or not row.get("final_100", "").strip():
                errors.append(f"Мөр {index}: код, улирал, хичээл эсвэл оноо хоосон.")
                skipped += 1
                continue
            student = db.execute(
                "SELECT id FROM users WHERE role = 'student' AND (student_code = ? OR username = ?)",
                (code, code),
            ).fetchone()
            sql = "SELECT id FROM academic_terms WHERE lower(name) = lower(?)"
            params: list = [term_name]
            if row.get("school_year", "").strip():
                sql += " AND lower(school_year) = lower(?)"
                params.append(row["school_year"].strip())
            sql += " ORDER BY start_date DESC LIMIT 1"
            term = db.execute(sql, params).fetchone()
            lesson = db.execute(
                "SELECT id FROM lessons WHERE lower(title) = lower(?) ORDER BY id DESC LIMIT 1",
                (lesson_title,),
            ).fetchone()
            if not student or not term or not lesson:
                errors.append(f"Мөр {index}: сурагч, улирал эсвэл хичээл олдсонгүй.")
                skipped += 1
                continue
            final_100 = max(0.0, min(100.0, _round1(row["final_100"])))
            ib_raw = row.get("ib_score", "").strip()
            ib_score = max(0.0, min(8.0, _round1(ib_raw))) if ib_raw else round(final_100 / 12.5, 1)
            db.execute(
                """INSERT INTO term_grade_overrides
                   (term_id, student_id, lesson_id, final_100, ib_score, note, updated_by, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(term_id, student_id, lesson_id)
                   DO UPDATE SET final_100 = excluded.final_100, ib_score = excluded.ib_score,
                                 note = excluded.note, updated_by = excluded.updated_by,
                                 updated_at = excluded.updated_at""",
                (term["id"], student["id"], lesson["id"], final_100, ib_score,
                 row.get("note", "").strip(), user["id"], int(time.time())),
            )
            imported += 1

    db.commit()
    result = {
        "kind": CSV_LABELS[kind], "imported": imported, "skipped": skipped, "errors": errors[:20],
    }
    return _transfer_page(request, user, db, result=result)
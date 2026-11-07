# EduPath IB & Odoo ERP Learning Platform — Developer & Copilot Guide

> **Төслийн нэр:** `tamir-ib` (EduPath)  
> **Зорилго:** 10-р ангийн сурагчдад Odoo ERP (Нийлүүлэлтийн сүлжээ, Агуулах, Санхүү, Татвар, Цалин)-ийн суурийг заах, интерактив HTML слайд үзүүлэх, Gemini/DeepSeek/Ollama ашиглан 8 асуулттай шалгалт үүсгэх боловсролын платформ. Сурагчийн оноог AI ашиглахгүй, хадгалсан зөв сонголттой тулган тооцно.

---

## 1. Технологийн стек (Tech Stack)

- **Backend:** Python 3.10+ (Current: Python 3.14 on Windows)
- **Web Framework:** FastAPI, Starlette, Uvicorn (ASGI)
- **AI Providers:** Gemini (`google-genai`), DeepSeek API, Ollama API
- **Quiz generation:** 8 multiple-choice questions, 2 per criterion, 2 options per question
- **Scoring:** Local correct-answer counts; 0–2 per criterion, 0–8 total
- **Database:** SQLite3 (`edupath.db` - хөнгөн, сервергүй)
- **Frontend / Templating:** Jinja2 Templates + Tailwind CSS (CDN)
- **Data Validation:** Pydantic v2 (JSON Schema enforced responses)
- **Image Processing:** Pillow (`PIL.Image`)

---

## 2. Төслийн хавтасны бүтэц (Directory Structure)

```text
tamir-ib/
├── app.py                  # FastAPI route, DB холболт
├── quiz_engine.py          # Shared quiz generation and deterministic scoring
├── create_quiz.py          # CLI entrypoint
├── development.md          # Төслийн заавар, Copilot Context
├── requirements.txt        # Шаардлагатай сангууд
├── edupath.db              # SQLite мэдээллийн сан (Auto-generated)
├── templates/              # Jinja2 HTML загварууд
│   ├── base.html           # Tailwind CSS бүхий суурь загвар, навигаци
│   ├── login.html          # Нэвтрэх & Шинээр бүртгүүлэх
│   ├── teacher.html        # Багшийн самбар: Слайд хуулах, AI provider сонгож quiz үүсгэх
│   ├── student.html        # Сурагчийн самбар: Хичээл үзэх, Шалгалт өгөх
│   ├── take_quiz.html      # 8 сонгох асуулт бүхий шалгалтын хуудас
│   └── result.html         # A-D оноо ба зөв/буруу хариултын дүн
└── uploads/                # Файл хадгалах хавтас
    ├── lessons/            # Багшийн хуулсан lesson.html слайдууд
    └── quiz_images/        # Квиз бэлдэхэд ашигласан диаграмм, схем зургууд
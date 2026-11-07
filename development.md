# EduPath IB & Odoo ERP Learning Platform — Developer & Copilot Guide

> **Төслийн нэр:** `tamir-ib` (EduPath)  
> **Зорилго:** 10-р ангийн сурагчдад Odoo ERP (Нийлүүлэлтийн сүлжээ, Агуулах, Санхүү, Татвар, Цалин)-ийн суурийг заах, багшийн бэлтгэсэн интерактив HTML слайдыг үзүүлэх, Gemini AI ашиглан зураг/prompt-оор шалгалт үүсгэх болон сурагчдын задгай хариултыг **IB (International Baccalaureate) Criteria A, B, C, D** шалгуураар автоматаар үнэлэх боловсролын платформ.

---

## 1. Технологийн стек (Tech Stack)

- **Backend:** Python 3.10+ (Current: Python 3.14 on Windows)
- **Web Framework:** FastAPI, Starlette, Uvicorn (ASGI)
- **AI / LLM SDK:** Албан ёсны шинэ `google-genai` SDK (`from google import genai`)
- **AI Model:** `gemini-2.5-flash` (Structured Outputs, Multimodal support)
- **Database:** SQLite3 (`edupath.db` - хөнгөн, сервергүй)
- **Frontend / Templating:** Jinja2 Templates + Tailwind CSS (CDN)
- **Data Validation:** Pydantic v2 (JSON Schema enforced responses)
- **Image Processing:** Pillow (`PIL.Image`)

---

## 2. Төслийн хавтасны бүтэц (Directory Structure)

```text
tamir-ib/
├── app.py                  # Бүх FastAPI route, DB холболт, Gemini дуудлага
├── development.md          # Төслийн заавар, Copilot Context
├── requirements.txt        # Шаардлагатай сангууд
├── edupath.db              # SQLite мэдээллийн сан (Auto-generated)
├── templates/              # Jinja2 HTML загварууд
│   ├── base.html           # Tailwind CSS бүхий суурь загвар, навигаци
│   ├── login.html          # Нэвтрэх & Шинээр бүртгүүлэх
│   ├── teacher.html        # Багшийн самбар: Слайд хуулах, Gemini Quiz үүсгэх
│   ├── student.html        # Сурагчийн самбар: Хичээл үзэх, Шалгалт өгөх
│   ├── take_quiz.html      # Сонгох болон задгай бичих асуулт бүхий шалгалтын хуудас
│   └── result.html         # IB A, B, C, D дүгнэлт, зөвлөмж бүхий дүнгийн хуудас
└── uploads/                # Файл хадгалах хавтас
    ├── lessons/            # Багшийн хуулсан lesson.html слайдууд
    └── quiz_images/        # Квиз бэлдэхэд ашигласан диаграмм, схем зургууд
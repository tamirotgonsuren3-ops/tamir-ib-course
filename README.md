# 🎓 EduPath IB & Odoo ERP Learning Platform

> **10-р ангийн сурагчдад зориулсан Бизнес, ERP систем, Нягтлан бодох бүртгэлийн интерактив сургалт ба IB шалгалтын систем.**

EduPath нь багшийн HTML хичээлүүдийг үзүүлж, Gemini, DeepSeek эсвэл Ollama-аар **IB Criteria A, B, C, D** шалгуурт хуваарилсан 8 асуулттай шалгалт үүсгэнэ. Сурагчийн дүнг зөв хариултын тоогоор локал тооцдог бөгөөд шалгалт өгөх үед AI дууддаггүй.

---

## 🌟 Гол онцлог боломжууд

### 👨‍🏫 Багшийн боломж:
- **Хичээлийн слайд (HTML) байршуулах:** Өмнө нь бэлтгэсэн интерактив танилцуулга (`lesson.html`, Odoo ERP, Татварын хичээл)-ийг системд шууд байршуулна.
- **AI provider сонгох:** Gemini, DeepSeek эсвэл локал Ollama-аар шалгалт үүсгэнэ.
- **Тогтмол бүтэцтэй шалгалт:** Нийт 8 сонгох асуулт; шалгуур бүрт 2 асуулт, тус бүр 2 сонголттой (1 зөв, 1 буруу).
- **Зураг ашиглах:** Шалгалт үүсгэх үед Gemini provider-той зураг хавсаргаж болно.

### 👩‍🎓 Сурагчийн боломж:
- **Интерактив слайд судлах:** Байршуулсан хичээлүүдийг бүтэн дэлгэцээр (Fullscreen) болон PDF хэлбэрээр үзэх.
- **Шалгалт өгөх:** 8 сонгох асуултад хариулж илгээнэ.
- **Шуурхай оноо:** A, B, C, D шалгуур тус бүр 0–2; нийт оноо 0–8. Зөвхөн хадгалсан зөв хариулттай тулгаж тооцно.

---

## 📐 IB (International Baccalaureate) Үнэлгээний Шалгуур

Шалгалт бүр 4 шалгуурын хүрээнд 2 асуулттай. Оноо нь шалгуур тус бүрт зөв хариулсан асуултын тоо (0–2) байна.

| Шалгуур | Нэршил | Тайлбар |
| :--- | :--- | :--- |
| **Criterion A** | Knowing and Understanding | Онолын ойлголт, нэр томьёо (ERP, НӨАТ, НДШ, Дебит/Кредит) |
| **Criterion B** | Inquiring and Planning | Асуудлыг шинжлэх, төлөвлөх, бизнес төлөвлөгөө боловсруулах |
| **Criterion C** | Processing and Evaluating | Тооцоолол хийх, шүүмжлэлтэй дүн шинжилгээ хийх чадвар |
| **Criterion D** | Real-world Application | Бодит амьдрал дээр хэрэглэх, эдийн засаг/нийгэмд үзүүлэх нөлөөг тусгах |

---

## 🛠️ Ашигласан технологиуд (Tech Stack)

- **Backend:** Python 3.10+, FastAPI, Starlette, Uvicorn
- **AI Engine:** Google GenAI (`google-genai`), DeepSeek API, Ollama API
- **Database:** SQLite3 (`edupath.db`)
- **Frontend:** Jinja2 Templates, Tailwind CSS (CDN)
- **Validation:** Pydantic v2 (Strict JSON Schema)
- **Image Processing:** Pillow (`PIL`)
- **HTTP client:** `requests`

---

## 🚀 Суулгах & Ажиллуулах заавар (Windows / macOS / Linux)

### 1. Төслийг хуулах (Clone / Create directory)
```bash
git clone https://github.com/your-username/tamir-ib.git
cd tamir-ib
```

### Ажиллуулах

```bash
python -m pip install -r requirements.txt
python -m uvicorn app:app --reload
```

Багшийн самбарт Gemini болон DeepSeek API key-гээ хадгална. Ollama ашиглахдаа Ollama service-ээ асааж, model татсан байна. `OLLAMA_BASE_URL` (default `http://localhost:11434`) болон `OLLAMA_MODEL` (default `llama3.1`) environment variable-аар тохируулж болно.

### CLI шалгалт үүсгэх

```bash
python create_quiz.py --file_input uploads/lessons/lesson.html --provider ollama --model llama3.1 --teacher_id 1
```

Provider сонголтууд: `gemini`, `deepseek`, `ollama`. Gemini болон DeepSeek key-г `--api_key`, `GEMINI_API_KEY` / `DEEPSEEK_API_KEY`, эсвэл веб дэх багшийн тохиргооноос авна. Ollama-д `OLLAMA_BASE_URL` ашиглана. PDF оролт зөвхөн Gemini provider-той ажиллана.
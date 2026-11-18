import json
import os
import re
import zipfile
import xml.etree.ElementTree as ET

import requests
from pydantic import BaseModel, Field

from quiz_engine import DEFAULT_MODELS

W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


class DocumentAssessment(BaseModel):
    A: int = Field(ge=0, le=2)
    B: int = Field(ge=0, le=2)
    C: int = Field(ge=0, le=2)
    D: int = Field(ge=0, le=2)
    feedback: str = ""
    strengths: str = ""
    improvements: str = ""


def _node_text(node) -> str:
    return "".join(t.text or "" for t in node.iter(f"{W_NS}t")).strip()


def extract_docx(path: str) -> dict:
    result = {
        "paragraphs": [],
        "tables": [],
        "chart_count": 0,
        "image_count": 0,
        "word_count": 0,
    }
    if not path or not os.path.exists(path):
        return result
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            result["chart_count"] = sum(
                1 for n in names if n.startswith("word/charts/chart") and n.endswith(".xml")
            )
            result["image_count"] = sum(1 for n in names if n.startswith("word/media/"))
            if "word/document.xml" not in names:
                return result
            root = ET.fromstring(archive.read("word/document.xml"))
            body = root.find(f"{W_NS}body")
            if body is None:
                return result
            for child in body:
                if child.tag == f"{W_NS}p":
                    text = _node_text(child)
                    if text:
                        result["paragraphs"].append(text)
                elif child.tag == f"{W_NS}tbl":
                    table = []
                    for row in child.findall(f"{W_NS}tr"):
                        table.append([_node_text(cell) for cell in row.findall(f"{W_NS}tc")])
                    if table:
                        result["tables"].append(table)
    except (zipfile.BadZipFile, ET.ParseError, OSError, KeyError) as exc:
        raise ValueError(f"Word файлыг уншиж чадсангүй: {exc}")

    full_text = "\n".join(result["paragraphs"])
    for table in result["tables"]:
        full_text += "\n" + "\n".join(" | ".join(row) for row in table)
    result["word_count"] = len(re.findall(r"\b\w+\b", full_text))
    return result


def _compact(content: dict, limit: int = 6000) -> str:
    text = "\n".join(content["paragraphs"])
    for table in content["tables"]:
        text += "\nTABLE:\n" + "\n".join(" | ".join(row) for row in table)
    text = text.strip() or "(хоосон баримт бичиг)"
    return text[:limit]


def _system_prompt() -> str:
    return (
        "You are an IB MYP data-analysis examiner. Assess a student's Word report using "
        "IB criteria A-D, each scored 0, 1 or 2. Criterion A: knowing and understanding of "
        "the data and concepts. B: inquiring and planning, quality of investigation design. "
        "C: processing and evaluating, correctness of calculations, tables and charts. "
        "D: communicating and applying findings to a real-world context. "
        "Reward evidence of tables, charts and written explanation. Penalize copied or empty "
        "work. Return only JSON with integer fields A, B, C, D (0-2) and short Mongolian "
        "strings feedback, strengths, improvements."
    )


def _user_prompt(assignment: dict, content: dict, csv_sample: str = "") -> str:
    return (
        f"Assignment title: {assignment.get('title', '')}\n"
        f"Assignment description: {assignment.get('description', '')}\n"
        f"Maximum raw score: {assignment.get('max_score', 20)}\n"
        f"Reference CSV (teacher sample):\n{(csv_sample or '')[:3000] or '(none)'}\n\n"
        f"Student document: paragraphs={len(content['paragraphs'])}, "
        f"tables={len(content['tables'])}, charts={content['chart_count']}, "
        f"images={content['image_count']}, words={content['word_count']}\n"
        f"--- DOCUMENT CONTENT ---\n{_compact(content)}\n--- END ---"
    )


def _parse_response(text: str) -> dict:
    match = re.search(r"\{.*\}", text or "", re.S)
    if not match:
        raise ValueError("AI returned no JSON object")
    data = json.loads(match.group(0))
    assessment = DocumentAssessment.model_validate(data)
    return assessment.model_dump()


def _request_provider(provider: str, api_key: str | None, model: str,
                      system_prompt: str, user_prompt: str) -> str:
    if provider == "gemini":
        if not api_key:
            raise ValueError("Gemini API key is required.")
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model=model or DEFAULT_MODELS["gemini"],
            contents=user_prompt,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                response_mime_type="application/json",
                response_schema=DocumentAssessment,
                temperature=0.2,
            ),
        )
        return response.text or ""

    if provider == "deepseek":
        if not api_key:
            raise ValueError("DeepSeek API key is required.")
        response = requests.post(
            "https://api.deepseek.com/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model or DEFAULT_MODELS["deepseek"],
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "response_format": {"type": "json_object"},
                "temperature": 0.2,
            },
            timeout=(10, 180),
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]

    if provider == "ollama":
        base_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
        response = requests.post(
            f"{base_url}/api/chat",
            json={
                "model": model or "llama3.1",
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "format": DocumentAssessment.model_json_schema(),
                "stream": False,
                "options": {"temperature": 0.2},
            },
            timeout=(10, 300),
        )
        response.raise_for_status()
        return response.json()["message"]["content"]

    raise ValueError(f"Unsupported provider: {provider}")


def grade_document(assignment: dict, docx_path: str, provider: str,
                   api_key: str | None, model: str = "",
                   csv_sample: str = "") -> dict:
    content = extract_docx(docx_path)
    raw_text = _request_provider(
        provider,
        api_key,
        model,
        _system_prompt(),
        _user_prompt(assignment, content, csv_sample),
    )
    assessment = _parse_response(raw_text)
    ib_total = sum(assessment[key] for key in ("A", "B", "C", "D"))
    max_score = max(1, int(assignment.get("max_score") or 20))
    scaled = round(ib_total / 8 * max_score, 1)
    assessment.update({
        "ib_total": ib_total,
        "criteria": {key: assessment[key] for key in ("A", "B", "C", "D")},
        "score": scaled,
        "max_score": max_score,
        "document_stats": {
            "paragraphs": len(content["paragraphs"]),
            "tables": len(content["tables"]),
            "charts": content["chart_count"],
            "words": content["word_count"],
        },
    })
    return assessment

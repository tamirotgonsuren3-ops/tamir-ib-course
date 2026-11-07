import os
from typing import Literal, Mapping, Sequence

import requests
from pydantic import BaseModel, Field, model_validator


CRITERIA = ("Criterion A", "Criterion B", "Criterion C", "Criterion D")
DEFAULT_MODELS = {
    "gemini": "gemini-2.5-flash",
    "deepseek": "deepseek-chat",
    "ollama": "llama3.1",
}


class QuizQuestion(BaseModel):
    id: int = Field(ge=1, le=8)
    question_type: Literal["multiple_choice"]
    target_criteria: Literal[
        "Criterion A", "Criterion B", "Criterion C", "Criterion D"
    ]
    question_text: str = Field(min_length=1)
    options: list[str] = Field(min_length=2, max_length=2)
    correct_option_index: int = Field(ge=0, le=1)

    @model_validator(mode="before")
    @classmethod
    def normalize_provider_fields(cls, value):
        if not isinstance(value, dict):
            return value

        normalized = dict(value)
        if "question_text" not in normalized:
            for alias in ("question", "text", "prompt"):
                if alias in normalized:
                    normalized["question_text"] = normalized[alias]
                    break
        if "question_type" not in normalized:
            normalized["question_type"] = "multiple_choice"
        if "target_criteria" not in normalized and "criterion" in normalized:
            criterion = str(normalized["criterion"]).strip()
            if criterion.upper() in {"A", "B", "C", "D"}:
                criterion = f"Criterion {criterion.upper()}"
            normalized["target_criteria"] = criterion
        return normalized


class GeneratedQuiz(BaseModel):
    quiz_title: str
    questions: list[QuizQuestion] = Field(min_length=8, max_length=8)

    @model_validator(mode="before")
    @classmethod
    def normalize_title(cls, value):
        if isinstance(value, dict) and "quiz_title" not in value and "title" in value:
            value = {**value, "quiz_title": value["title"]}
        return value


def validate_quiz(quiz: GeneratedQuiz) -> GeneratedQuiz:
    question_ids = [question.id for question in quiz.questions]
    if set(question_ids) != set(range(1, 9)):
        raise ValueError("Quiz question ids must be unique and range from 1 to 8.")

    counts = {criterion: 0 for criterion in CRITERIA}
    for question in quiz.questions:
        counts[question.target_criteria] += 1
        if question.options[0].strip() == question.options[1].strip():
            raise ValueError(f"Question {question.id} has duplicate answer choices.")

    if any(count != 2 for count in counts.values()):
        raise ValueError("A quiz must contain exactly two questions for each criterion.")
    return quiz


def _system_prompt() -> str:
    return (
        "Create an objective multiple-choice quiz for a grade 10 business and ERP course. "
        "Return only JSON with these exact fields: quiz_title; questions; and for each question "
        "id, question_type, target_criteria, question_text, options, correct_option_index. "
        "Set question_type to multiple_choice and target_criteria to Criterion A, Criterion B, "
        "Criterion C, or Criterion D. The quiz must contain exactly 8 "
        "questions, with exactly 2 questions for each of Criterion A, Criterion B, "
        "Criterion C, and Criterion D. Every question must have exactly two distinct "
        "answer choices: one factually correct and one clearly incorrect. Set "
        "correct_option_index to 0 or 1 for the correct choice. Use question ids 1 through 8 "
        "exactly once. Do not create written-response questions."
    )


def _request_openai_compatible(
    provider: str, api_key: str | None, model: str, prompt: str
) -> str:
    if provider == "deepseek":
        if not api_key:
            raise ValueError("DeepSeek API key is required.")
        url = "https://api.deepseek.com/chat/completions"
        headers = {"Authorization": f"Bearer {api_key}"}
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": _system_prompt()},
                {"role": "user", "content": prompt},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
        }
        response = requests.post(url, headers=headers, json=payload, timeout=(10, 180))
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]

    if provider == "ollama":
        base_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": _system_prompt()},
                {"role": "user", "content": prompt},
            ],
            "format": GeneratedQuiz.model_json_schema(),
            "stream": False,
            "options": {"temperature": 0.2},
        }
        response = requests.post(f"{base_url}/api/chat", json=payload, timeout=(10, 300))
        response.raise_for_status()
        return response.json()["message"]["content"]

    raise ValueError(f"Unsupported quiz provider: {provider}")


def generate_quiz(
    provider: str,
    prompt: str,
    title: str,
    api_key: str | None = None,
    model: str | None = None,
    image_path: str | None = None,
    pdf_path: str | None = None,
) -> GeneratedQuiz:
    if provider not in DEFAULT_MODELS:
        raise ValueError("Provider must be gemini, deepseek, or ollama.")
    if provider != "gemini" and (image_path or pdf_path):
        raise ValueError("Image and PDF input are currently supported with Gemini only.")

    selected_model = model
    if not selected_model and provider == "ollama":
        selected_model = os.environ.get("OLLAMA_MODEL")
    selected_model = selected_model or DEFAULT_MODELS[provider]
    full_prompt = f"Quiz title: {title}\nTeacher instructions and lesson content:\n{prompt}"

    if provider == "gemini":
        if not api_key:
            raise ValueError("Gemini API key is required.")
        from google import genai
        from google.genai import types

        contents = []
        if pdf_path:
            contents.append(genai.Client(api_key=api_key).files.upload(file=pdf_path))
        if image_path:
            from PIL import Image

            contents.append(Image.open(image_path))
        contents.append(f"{_system_prompt()}\n\n{full_prompt}")
        response = genai.Client(api_key=api_key).models.generate_content(
            model=selected_model,
            contents=contents,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=GeneratedQuiz,
                temperature=0.2,
            ),
        )
        response_text = response.text
    else:
        response_text = _request_openai_compatible(
            provider, api_key, selected_model, full_prompt
        )

    return validate_quiz(GeneratedQuiz.model_validate_json(response_text))


def score_quiz(
    questions: Sequence[Mapping[str, object]], answers: Mapping[str, str]
) -> tuple[list[dict[str, object]], list[dict[str, object]], int]:
    scores = {criterion: 0 for criterion in CRITERIA}
    responses = []

    for question in questions:
        question_id = question["id"]
        criterion = str(question.get("target_criteria", ""))
        submitted = answers.get(f"q_{question_id}", "").strip()
        try:
            selected_index = int(submitted)
        except (TypeError, ValueError):
            selected_index = None

        options = question.get("options") or []
        correct_index = question.get("correct_option_index")
        is_correct = selected_index is not None and correct_index is not None and selected_index == int(correct_index)
        if is_correct and criterion in scores:
            scores[criterion] += 1

        responses.append({
            "id": question_id,
            "target_criteria": criterion,
            "question_text": question["question_text"],
            "question_type": question.get("question_type", "multiple_choice"),
            "student_answer": submitted,
            "selected_option_text": options[selected_index] if selected_index is not None and 0 <= selected_index < len(options) else "",
            "correct_option_text": options[int(correct_index)] if correct_index is not None and 0 <= int(correct_index) < len(options) else "",
            "is_correct": bool(is_correct),
        })

    criterion_scores = [
        {"criterion": criterion, "score": scores[criterion], "max_score": 2}
        for criterion in CRITERIA
    ]
    return responses, criterion_scores, sum(scores.values())
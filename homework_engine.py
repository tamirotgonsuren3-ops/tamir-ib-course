"""Homework grading helpers.

Student code is executed in a separate, isolated Python process with a timeout.
Unit tests are plain Python statements (usually ``assert`` calls) that are run
against the student's code.  When Ollama is available it is used only for
qualitative feedback; scores are deterministic whenever tests are supplied.
"""

import json
import math
import os
import re
import subprocess
import sys
import tempfile
from typing import Any, Optional


DEFAULT_MAX_SCORE = 10


_HARNESS = r"""
import json
import sys


def main():
    input_path, output_path = sys.argv[1], sys.argv[2]
    with open(input_path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)

    namespace = {"__name__": "__main__"}
    results = []
    try:
        exec(compile(payload["code"], "<student>", "exec"), namespace)
        loaded = True
        load_error = ""
    except BaseException as exc:
        loaded = False
        load_error = f"{type(exc).__name__}: {exc}"

    if loaded:
        for test in payload["tests"]:
            try:
                exec(
                    compile(test["code"], f"<test:{test['name']}>", "exec"),
                    namespace,
                )
                results.append({"name": test["name"], "passed": True, "error": ""})
            except BaseException as exc:
                results.append(
                    {
                        "name": test["name"],
                        "passed": False,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )

    with open(output_path, "w", encoding="utf-8") as handle:
        json.dump(
            {
                "loaded": loaded,
                "load_error": load_error,
                "results": results,
            },
            handle,
            ensure_ascii=False,
        )


main()
"""


def _limit_resources():
    """Best-effort memory/CPU limits on POSIX (ignored on Windows)."""
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (128 * 1024 * 1024, 128 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_CPU, (5, 5))
    except Exception:
        pass


def clamp_score(value: Any, max_score: int) -> int:
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        return 0
    return max(0, min(max_score, number))


def answers_match(submitted: Any, expected: Any) -> bool:
    submitted_text = str(submitted if submitted is not None else "").strip()
    expected_text = str(expected if expected is not None else "").strip()
    if not expected_text:
        return False
    if submitted_text.casefold() == expected_text.casefold():
        return True

    try:
        submitted_number = float(submitted_text.replace(",", "."))
        expected_number = float(expected_text.replace(",", "."))
        if math.isclose(submitted_number, expected_number, rel_tol=1e-9, abs_tol=1e-9):
            return True
    except (TypeError, ValueError):
        pass

    normalize_spaces = lambda value: re.sub(r"\s+", " ", value).strip().casefold()
    return normalize_spaces(submitted_text) == normalize_spaces(expected_text)


def grade_fixed_answer(
    answer: Any,
    expected: Any,
    max_score: int = DEFAULT_MAX_SCORE,
    description: str = "",
    use_ai: bool = False,
) -> dict:
    max_score = max(1, max_score or DEFAULT_MAX_SCORE)
    submitted_text = str(answer if answer is not None else "").strip()
    if answers_match(submitted_text, expected):
        score = max_score
        explanation = (
            f"Зөв хариулт! Таны хариулт: {submitted_text}.\n"
            f"Оноо: {score}/{max_score}."
        )
    else:
        score = 0
        explanation = (
            "Хариулт буруу байна.\n"
            f"Таны хариулт: {submitted_text or '(хоосон)'}.\n"
            "Бодолтоо дахин шалгаад хариултаа дахин илгээнэ үү."
        )
    return {
        "status": "done",
        "score": score,
        "explanation": explanation,
        "test_results": {"loaded": True, "results": []},
    }


def run_unit_tests(code: str, tests: list[dict[str, str]], timeout_seconds: int = 5) -> dict:
    """Run student code and tests in a subprocess. Returns a result dict."""
    payload_tests = []
    for index, test in enumerate(tests, start=1):
        test_code = (test.get("test_code") or "").strip()
        if not test_code:
            continue
        payload_tests.append(
            {
                "name": (test.get("name") or f"Тест {index}").strip(),
                "code": test_code,
            }
        )

    with tempfile.TemporaryDirectory(prefix="edupath_hw_") as workdir:
        input_path = os.path.join(workdir, "input.json")
        output_path = os.path.join(workdir, "output.json")
        harness_path = os.path.join(workdir, "harness.py")

        with open(input_path, "w", encoding="utf-8") as handle:
            json.dump({"code": code or "", "tests": payload_tests}, handle, ensure_ascii=False)
        with open(harness_path, "w", encoding="utf-8") as handle:
            handle.write(_HARNESS)

        env = {
            "PATH": os.environ.get("PATH", ""),
            "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
            "TEMP": workdir,
            "TMP": workdir,
            "PYTHONIOENCODING": "utf-8",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        kwargs: dict[str, Any] = {
            "cwd": workdir,
            "env": env,
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "timeout": timeout_seconds,
        }
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            kwargs["preexec_fn"] = _limit_resources

        try:
            subprocess.run(
                [sys.executable, "-I", "-B", harness_path, input_path, output_path],
                **kwargs,
            )
        except subprocess.TimeoutExpired:
            return {
                "loaded": True,
                "load_error": "",
                "timed_out": True,
                "results": [
                    {
                        "name": test["name"],
                        "passed": False,
                        "error": f"Хугацаа хэтэрлээ ({timeout_seconds} сек).",
                    }
                    for test in payload_tests
                ],
            }
        except Exception as exc:
            return {
                "loaded": False,
                "load_error": f"Шалгагч ажилласангүй: {type(exc).__name__}: {exc}",
                "timed_out": False,
                "results": [],
            }

        if not os.path.exists(output_path):
            return {
                "loaded": False,
                "load_error": "Сурагчийн код ажиллуулах үед алдаа гарлаа.",
                "timed_out": False,
                "results": [],
            }

        try:
            with open(output_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except Exception as exc:
            return {
                "loaded": False,
                "load_error": f"Үр дүнг уншиж чадсангүй: {exc}",
                "timed_out": False,
                "results": [],
            }

    data.setdefault("timed_out", False)
    return data


def _extract_json(text: str) -> Optional[dict]:
    text = (text or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    try:
        return json.loads(text)
    except Exception:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except Exception:
            return None
    return None


def ai_feedback(
    description: str,
    code: str,
    test_results: list[dict],
    max_score: int,
    timeout_seconds: int = 30,
) -> Optional[dict]:
    """Ask the local Ollama instance for feedback. Returns None when unavailable."""
    if os.environ.get("OLLAMA_DISABLE_HOMEWORK_AI"):
        return None
    try:
        import requests
    except Exception:
        return None

    base_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
    model = os.environ.get("OLLAMA_MODEL", "llama3.2:3b")
    passed = sum(1 for item in test_results if item.get("passed"))
    prompt = (
        "Чи Python хичээлийн багшийн туслах. Сурагчийн кодыг үнэл.\n"
        f"Даалгавар: {description}\n"
        "Сурагчийн код:\n```python\n"
        f"{code}\n```\n"
        f"Нэгж тестийн үр дүн: {passed}/{len(test_results)} тест давсан.\n"
        f"Дээд оноо: {max_score}\n\n"
        "Дүрэм:\n"
        "- Бүрэн шийдлийг бичиж бүү өг; алдаа гарсан газарт чиглүүлэх зөвлөмж өг.\n"
        "- Зөвхөн Монгол хэлээр хариул.\n"
        "- Дараах JSON форматаар л хариул: "
        '{"score": <0..maxScore бүхэл тоо>, "explanation": "<Монгол тайлбар>"}'
    )
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "format": "json",
        "stream": False,
        "options": {"temperature": 0.2},
    }
    try:
        response = requests.post(
            f"{base_url}/api/chat",
            json=payload,
            timeout=(2, timeout_seconds),
        )
        response.raise_for_status()
        content = response.json().get("message", {}).get("content", "")
    except Exception:
        return None

    data = _extract_json(content)
    if not isinstance(data, dict):
        return None
    explanation = str(data.get("explanation") or "").strip()
    if not explanation:
        return None
    return {
        "explanation": explanation,
        "score": clamp_score(data.get("score"), max_score),
    }


def _test_explanation(results: list[dict], passed: int, score: int, max_score: int) -> str:
    total = len(results)
    lines = [f"Тестийн дүн: {passed}/{total} давсан. Оноо: {score}/{max_score}."]
    if passed == total:
        lines.append("Бүх тест амжилттай давлаа. Сайн ажиллалаа!")
        return "\n".join(lines)

    lines.append("Анхаарах хэсгүүд:")
    for item in results:
        if item.get("passed"):
            continue
        error = (item.get("error") or "Тест биелсэнгүй.").strip()
        lines.append(f"- {item.get('name', 'Тест')}: {error}")
    lines.append("Алдаа гарсан хэсгээ дахин шалгаж, тестийн нөхцөлийг хангана уу.")
    return "\n".join(lines)


def grade_submission(
    code: str,
    tests: list[dict[str, str]],
    description: str,
    max_score: int = DEFAULT_MAX_SCORE,
    use_ai: bool = True,
) -> dict:
    """Grade a submission and return status/score/explanation/test results."""
    max_score = max_score or DEFAULT_MAX_SCORE
    if max_score < 1:
        max_score = 1

    if not tests:
        feedback = ai_feedback(description, code, [], max_score) if use_ai else None
        if feedback:
            return {
                "status": "done",
                "score": clamp_score(feedback.get("score"), max_score),
                "explanation": feedback["explanation"],
                "test_results": {"loaded": True, "results": []},
            }
        return {
            "status": "pending",
            "score": None,
            "explanation": "Энэ даалгаварт автомат шалгах тест байхгүй тул багш гараар үнэлнэ.",
            "test_results": {"loaded": True, "results": []},
        }

    result = run_unit_tests(code, tests)
    test_results = result.get("results", [])

    if not result.get("loaded"):
        error = result.get("load_error") or "Тодорхойгүй алдаа."
        return {
            "status": "done",
            "score": 0,
            "explanation": (
                "Таны код ажилласангүй. Синтакс болон хувьсагчийн нэрээ шалгана уу.\n"
                f"Алдаа: {error}"
            ),
            "test_results": result,
        }

    passed = sum(1 for item in test_results if item.get("passed"))
    total = len(test_results)
    score = clamp_score(max_score * passed / total, max_score) if total else 0
    explanation = _test_explanation(test_results, passed, score, max_score)

    if use_ai:
        feedback = ai_feedback(description, code, test_results, max_score)
        if feedback:
            explanation += "\n\nAI зөвлөмж: " + feedback["explanation"]

    return {
        "status": "done",
        "score": score,
        "explanation": explanation,
        "test_results": result,
    }

"""Quiz Q&A extraction + PDF rendering.

Two-phase design:

1) `extract_quiz_qa(raw)` — parse the response from `getLessonTest`. It walks
   the JSON looking for objects that look like questions (have `question` /
   `text` plus a list of answers), and for each answer looks for a "correct"
   flag under common naming conventions. If no flags are found inline, falls
   back to the brute-force submit/retry loop (see `brute_force_quiz`).

2) `render_pdf(course_name, sections, out_path)` — write the collected Q&A
   sections to a clean PDF using fpdf2.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fpdf import FPDF

from .api import QuizQA, Test

log = logging.getLogger(__name__)


@dataclass
class QuizSection:
    module_number: int | None
    module_title: str
    qa: list[QuizQA]


def slugify(text: str, max_len: int = 80) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^\w\s-]", "", text).strip()
    text = re.sub(r"\s+", "_", text)
    return text[:max_len] or "untitled"


def clean_text(text: str) -> str:
    """Strip HTML tags and decode common entities."""
    text = re.sub(r"<[^>]+>", " ", text)
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
                .replace("&lt;", "<").replace("&gt;", ">")
                .replace("&quot;", '"').replace("&#039;", "'")
                .replace("&#39;", "'"))
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------- parsing

QUESTION_TEXT_KEYS = ("question", "question_text", "text", "title", "wording", "label", "body")
ANSWER_TEXT_KEYS = ("answer", "answer_text", "text", "label", "title", "value", "wording")
CORRECT_FLAG_KEYS = ("correct", "is_correct", "isCorrect", "correctAnswer",
                     "is_right", "isRight", "right", "score", "value")


def _first_str(obj: dict, keys) -> str | None:
    for k in keys:
        v = obj.get(k)
        if isinstance(v, str) and v.strip():
            return clean_text(v)
    return None


def _is_correct(obj: dict) -> bool:
    for k in CORRECT_FLAG_KEYS:
        v = obj.get(k)
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)) and v in (1, 100):
            return True
        if isinstance(v, str) and v.strip().lower() in ("1", "true", "yes", "y", "correct"):
            return True
    return False


def _find_questions(node: Any) -> list[dict]:
    """Find dicts that look like quiz questions (have a 'question'-ish text
    field and a list of answer-like dicts)."""
    out: list[dict] = []

    def walk(n):
        if isinstance(n, dict):
            qtxt = _first_str(n, QUESTION_TEXT_KEYS)
            # Look for a child list with answer-shaped dicts
            for v in n.values():
                if isinstance(v, list) and v and isinstance(v[0], dict):
                    sample = v[0]
                    has_answer_text = any(k in sample for k in ANSWER_TEXT_KEYS)
                    if qtxt and has_answer_text:
                        out.append({"question": qtxt, "answers": v})
                        break
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)

    walk(node)
    return out


def extract_quiz_qa(raw: Any) -> list[QuizQA]:
    """Parse a `getLessonTest` response into QuizQA items.

    Mercatorum's payload shape:
        {"data": {"id": <lessonId>, "testSource": [{
            "id_question": "1",
            "question": "...",
            "paragraph": "...",            # sub-topic
            "titolo_videolezione": "...",
            "correct_answer": "4",           # 1-indexed string
            "answers": [
                {"id_answer": 0, "answer": "..."},
                ...
            ]
        }, ...]}}
    """
    out: list[QuizQA] = []
    container = raw.get("data") if isinstance(raw, dict) and "data" in raw else raw
    questions = None
    if isinstance(container, dict):
        for key in ("testSource", "questions", "test_source", "items"):
            v = container.get(key)
            if isinstance(v, list):
                questions = v
                break

    if questions:
        for q in questions:
            if not isinstance(q, dict):
                continue
            question_text = clean_text(_first_str(q, QUESTION_TEXT_KEYS) or "")
            if not question_text:
                continue
            answers = q.get("answers")
            if not isinstance(answers, list):
                continue
            raw_correct = q.get("correct_answer", q.get("correctAnswer"))
            try:
                correct_idx = int(raw_correct) - 1 if raw_correct is not None else None
            except (TypeError, ValueError):
                correct_idx = None

            all_a: list[str] = []
            for a in answers:
                if isinstance(a, dict):
                    txt = clean_text(a.get("answer") or _first_str(a, ANSWER_TEXT_KEYS) or "")
                    if txt:
                        all_a.append(txt)
            correct: list[str] = []
            if correct_idx is not None and 0 <= correct_idx < len(all_a):
                correct.append(all_a[correct_idx])

            paragraph = clean_text(q.get("paragraph") or "") or None
            subtopic = clean_text(q.get("titolo_videolezione") or "") or None

            if correct:
                out.append(QuizQA(
                    question=question_text,
                    correct_answers=correct,
                    all_answers=all_a,
                    paragraph=paragraph,
                    subtopic=subtopic,
                ))
        if out:
            return out

    return _extract_quiz_qa_generic(raw)


def _extract_quiz_qa_generic(raw: Any) -> list[QuizQA]:
    """Original generic walker — used when the Mercatorum-specific format
    isn't detected. Looks for `correct`/`is_correct` flags on each answer."""
    questions = _find_questions(raw)
    out: list[QuizQA] = []
    for q in questions:
        correct = []
        all_a = []
        for ans in q["answers"]:
            txt = _first_str(ans, ANSWER_TEXT_KEYS) if isinstance(ans, dict) else None
            if not txt:
                continue
            all_a.append(txt)
            if isinstance(ans, dict) and _is_correct(ans):
                correct.append(txt)
        if correct:
            out.append(QuizQA(question=q["question"], correct_answers=correct, all_answers=all_a))
    return out


# ------------------------------------------------------- brute-force fallback

def brute_force_quiz(api, course_code: str, test: Test) -> list[QuizQA]:
    """Last-resort: when correct flags aren't exposed inline, simulate the UI
    'try each option → submit → read results → repeat' loop via API.

    This function is a placeholder pending discovery of the submit/result
    endpoints. The expected flow is:
        1) GET/POST start_test → returns question list + attempt_token
        2) POST submit_answer per question
        3) POST finalize → returns per-question correctness
        4) Iterate over answer positions 0..N-1, aggregate.
    """
    log.warning("brute-force quiz extraction not yet implemented for %s/%s",
                course_code, test.lp_item_id)
    return []


# --------------------------------------------------------------------- PDF

class _QuizPDF(FPDF):
    def __init__(self, course_name: str):
        super().__init__()
        self.course_name = course_name
        self.set_auto_page_break(auto=True, margin=15)
        self.set_margins(15, 15, 15)

    def header(self):
        self.set_font("Helvetica", "B", 14)
        self.set_text_color(185, 28, 28)              # brand red
        self.cell(0, 8, self.course_name, ln=True)
        self.set_draw_color(220, 220, 220)
        self.set_line_width(0.3)
        self.line(15, self.get_y(), 195, self.get_y())
        self.ln(4)
        self.set_text_color(0, 0, 0)

    def footer(self):
        self.set_y(-12)
        self.set_font("Helvetica", "I", 8)
        self.set_text_color(120, 120, 120)
        self.cell(0, 8, f"Pag. {self.page_no()}", align="R")


def render_pdf(course_name: str, sections: list[QuizSection], out_path: Path) -> None:
    """Render the collected quiz Q&A sections to a single PDF."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pdf = _QuizPDF(course_name)
    pdf.add_page()

    if not sections:
        pdf.set_font("Helvetica", "I", 11)
        pdf.multi_cell(0, 7, "Nessun quiz trovato per questo corso.")
        pdf.output(str(out_path))
        return

    total_q = sum(len(s.qa) for s in sections)
    pdf.set_font("Helvetica", "I", 9)
    pdf.set_text_color(120, 120, 120)
    pdf.multi_cell(0, 5, _safe(f"{len(sections)} moduli · {total_q} domande"))
    pdf.ln(3)

    for section in sections:
        prefix = f"{section.module_number:02d}. " if section.module_number is not None else ""
        pdf.set_font("Helvetica", "B", 13)
        pdf.set_text_color(40, 40, 40)
        pdf.multi_cell(0, 8, _safe(f"{prefix}{section.module_title}"))
        pdf.set_draw_color(220, 220, 220)
        pdf.set_line_width(0.2)
        pdf.line(15, pdf.get_y(), 195, pdf.get_y())
        pdf.ln(3)

        for i, qa in enumerate(section.qa, start=1):
            pdf.set_font("Helvetica", "B", 10)
            pdf.set_text_color(0, 0, 0)
            pdf.multi_cell(0, 6, _safe(f"Q{i}. {qa.question}"))

            pdf.set_font("Helvetica", "B", 10)
            pdf.set_text_color(34, 134, 58)
            for a in qa.correct_answers:
                pdf.set_x(pdf.l_margin + 5)
                pdf.multi_cell(0, 6, _safe(f"» {a}"))
            pdf.set_text_color(0, 0, 0)
            pdf.set_font("Helvetica", "", 10)
            pdf.ln(1.5)
        pdf.ln(4)

    pdf.output(str(out_path))


# --- Latin-1 safety: built-in fpdf2 fonts can't render arbitrary Unicode.
_CHAR_REPLACEMENTS = {
    "‘": "'", "’": "'",            # ‘ ’
    "“": '"', "”": '"',            # “ ”
    "—": "-", "–": "-",            # — –
    "…": "...",                          # …
    "→": "->", "←": "<-",          # → ←
    "•": "*", "‣": "*",            # • ‣
    "✓": ">", "✔": ">",            # ✓ ✔
    "✗": "x", "✘": "x",            # ✗ ✘
    "≠": "!=", "≤": "<=", "≥": ">=",  # ≠ ≤ ≥
    " ": " ",                            # NBSP
}


def _safe(s: str) -> str:
    """Map common Unicode chars into Latin-1, then replace anything left.
    Also inserts space break-points inside very long whitespace-free runs so
    fpdf2's WORD wrapper has somewhere to break URLs/formulas."""
    if not s:
        return ""
    for k, v in _CHAR_REPLACEMENTS.items():
        s = s.replace(k, v)
    s = _break_long_tokens(s)
    return s.encode("latin-1", "replace").decode("latin-1")


def _break_long_tokens(text: str, max_len: int = 60) -> str:
    """Insert a space after every `max_len` consecutive non-whitespace chars."""
    return re.sub(r"(\S{" + str(max_len) + r"})(?=\S)", r"\1 ", text)


def _ascii(s: str) -> str:
    """Legacy alias retained for any external callers."""
    return _safe(s)

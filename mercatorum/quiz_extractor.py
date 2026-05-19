"""Quiz Q&A extraction + PDF rendering."""

from __future__ import annotations

import logging
import re
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
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
    text = str(text or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
                .replace("&lt;", "<").replace("&gt;", ">")
                .replace("&quot;", '"').replace("&#039;", "'")
                .replace("&#39;", "'"))
    return re.sub(r"\s+", " ", text).strip()


QUESTION_TEXT_KEYS = ("question", "question_text", "text", "title", "wording", "label", "body")
ANSWER_TEXT_KEYS = ("answer", "answer_text", "text", "label", "title", "value", "wording")
IMG_SRC_RE = re.compile(r"<img\b[^>]*?\bsrc=[\"']([^\"']+)[\"']", re.IGNORECASE)


def _first_str(obj: dict, keys) -> str | None:
    for k in keys:
        v = obj.get(k)
        if isinstance(v, str) and v.strip():
            return clean_text(v)
    return None


def _first_raw_str(obj: dict, keys) -> str:
    for k in keys:
        v = obj.get(k)
        if isinstance(v, str) and v.strip():
            return v
    return ""


def _extract_image_urls(html: str) -> list[str]:
    out: list[str] = []
    for src in IMG_SRC_RE.findall(html or ""):
        src = src.strip()
        if src.startswith("//"):
            src = f"https:{src}"
        if src and src not in out:
            out.append(src)
    return out


def extract_quiz_qa(raw: Any) -> list[QuizQA]:
    """Parse Mercatorum's `getLessonTest` response into QuizQA items."""
    out: list[QuizQA] = []
    container = raw.get("data") if isinstance(raw, dict) and "data" in raw else raw
    questions = None
    if isinstance(container, dict):
        for key in ("testSource", "questions", "test_source", "items"):
            v = container.get(key)
            if isinstance(v, list):
                questions = v
                break
    if not questions:
        return out

    for q in questions:
        if not isinstance(q, dict):
            continue
        question_raw = _first_raw_str(q, QUESTION_TEXT_KEYS)
        question_text = clean_text(question_raw)
        question_images = _extract_image_urls(question_raw)
        if not question_text and not question_images:
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
        all_answer_images: list[list[str]] = []
        for a in answers:
            if isinstance(a, dict):
                answer_raw = str(a.get("answer") or _first_raw_str(a, ANSWER_TEXT_KEYS) or "")
                txt = clean_text(answer_raw)
                imgs = _extract_image_urls(answer_raw)
                if txt or imgs:
                    all_a.append(txt)
                    all_answer_images.append(imgs)
        correct: list[str] = []
        correct_images: list[str] = []
        if correct_idx is not None and 0 <= correct_idx < len(all_a):
            correct.append(all_a[correct_idx])
            correct_images = all_answer_images[correct_idx]

        if correct or correct_images:
            out.append(QuizQA(
                question=question_text,
                correct_answers=correct,
                all_answers=all_a,
                question_images=question_images,
                correct_answer_images=correct_images,
                all_answer_images=all_answer_images,
                paragraph=clean_text(q.get("paragraph") or "") or None,
                subtopic=clean_text(q.get("titolo_videolezione") or "") or None,
            ))
    return out


# --- PDF rendering --------------------------------------------------------

_CHAR_REPLACEMENTS = {
    "‘": "'", "’": "'",
    "“": '"', "”": '"',
    "—": "-", "–": "-",
    "…": "...",
    "→": "->", "←": "<-",
    "•": "*", "‣": "*",
    "✓": ">", "✔": ">",
    "✗": "x", "✘": "x",
    "≠": "!=", "≤": "<=", "≥": ">=",
    " ": " ",
}


def _break_long_tokens(text: str, max_len: int = 60) -> str:
    return re.sub(r"(\S{" + str(max_len) + r"})(?=\S)", r"\1 ", text)


def _safe(s: str) -> str:
    if not s:
        return ""
    for k, v in _CHAR_REPLACEMENTS.items():
        s = s.replace(k, v)
    s = _break_long_tokens(s)
    return s.encode("latin-1", "replace").decode("latin-1")


def _image_label(url: str) -> str:
    parsed = urlparse(url)
    name = parsed.path.rsplit("/", 1)[-1]
    return name or "immagine"


def _download_image(url: str, dest_dir: Path, session: requests.Session | None = None) -> Path | None:
    if not url.startswith(("http://", "https://")):
        return None
    try:
        client = session or requests
        r = client.get(url, timeout=20)
        r.raise_for_status()
        content_type = r.headers.get("content-type", "").split(";", 1)[0].lower()
        suffix = Path(urlparse(url).path).suffix.lower()
        if content_type == "image/jpeg" and suffix not in {".jpg", ".jpeg"}:
            suffix = ".jpg"
        elif content_type == "image/png" and suffix != ".png":
            suffix = ".png"
        elif content_type == "image/gif" and suffix != ".gif":
            suffix = ".gif"
        if suffix not in {".jpg", ".jpeg", ".png", ".gif"}:
            return None
        out = dest_dir / f"quiz_image_{abs(hash(url))}{suffix}"
        out.write_bytes(r.content)
        return out
    except Exception as e:
        log.debug("🖼️ quiz image fetch failed: file=%s error=%s", _image_label(url), e)
        return None


class _QuizPDF(FPDF):
    def __init__(self, course_name: str):
        super().__init__()
        self.course_name = course_name
        self.set_auto_page_break(auto=True, margin=15)
        self.set_margins(15, 15, 15)

    def header(self):
        self.set_font("Helvetica", "B", 14)
        self.set_text_color(185, 28, 28)
        self.cell(0, 8, _safe(self.course_name), ln=True)
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


def render_pdf(
    course_name: str,
    sections: list[QuizSection],
    out_path: Path,
    image_session: requests.Session | None = None,
) -> None:
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

    with tempfile.TemporaryDirectory(prefix="automercatorum_quiz_images_") as tmp:
        image_dir = Path(tmp)

        def draw_images(urls: list[str], indent: float = 5) -> None:
            for url in urls:
                img_path = _download_image(url, image_dir, image_session)
                if img_path is None:
                    pdf.set_x(pdf.l_margin + indent)
                    pdf.set_font("Helvetica", "I", 8)
                    pdf.set_text_color(120, 120, 120)
                    pdf.multi_cell(0, 5, _safe(f"[immagine non disponibile: {_image_label(url)}]"))
                    continue
                try:
                    if pdf.get_y() > 235:
                        pdf.add_page()
                    pdf.image(str(img_path), x=pdf.l_margin + indent, w=90)
                    pdf.ln(2)
                except Exception as e:
                    log.debug("🖼️ quiz image render failed: file=%s error=%s", img_path.name, e)
                    pdf.set_x(pdf.l_margin + indent)
                    pdf.set_font("Helvetica", "I", 8)
                    pdf.set_text_color(120, 120, 120)
                    pdf.multi_cell(0, 5, _safe(f"[immagine non renderizzabile: {_image_label(url)}]"))

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
                draw_images(qa.question_images)

                pdf.set_font("Helvetica", "B", 10)
                pdf.set_text_color(34, 134, 58)
                for a in qa.correct_answers:
                    if a:
                        pdf.set_x(pdf.l_margin + 5)
                        pdf.multi_cell(0, 6, _safe(f"> {a}"))
                draw_images(qa.correct_answer_images)
                pdf.set_text_color(0, 0, 0)
                pdf.set_font("Helvetica", "", 10)
                pdf.ln(1.5)
            pdf.ln(4)

    pdf.output(str(out_path))


def brute_force_quiz(api, course_code: str, test):
    """Backward-compat stub. The Tools-side extractor now reads inline correct
    flags + images from `getLessonTest` directly, so the brute-force fallback
    is no longer reachable in practice. Returning [] keeps the standalone
    app.py import surface stable without re-introducing dead code."""
    log.debug("brute_force_quiz called for %s/%s — returning empty (no fallback path)",
              course_code, getattr(test, "lp_item_id", "?"))
    return []

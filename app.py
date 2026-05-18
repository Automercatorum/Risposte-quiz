"""Automercatorum Risposte Quiz — pywebview entry point.

Native window that extracts the correct answers to practice quizzes ("test di
fine lezione") and saves them as a PDF answer key per course.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading
from pathlib import Path

import webview

from mercatorum.api import AuthError, MercatorumAPI
from mercatorum.creds_store import CredentialsStore
from mercatorum.quiz_extractor import (
    QuizSection,
    brute_force_quiz,
    extract_quiz_qa,
    render_pdf,
    slugify,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("app")

ROOT = Path(__file__).resolve().parent
AUTH_DIR = ROOT / ".auth"
OUTPUT = ROOT / "output"
UI_INDEX = ROOT / "ui" / "index.html"


class JsApi:
    def __init__(self) -> None:
        self.store = CredentialsStore(AUTH_DIR)
        self.api: MercatorumAPI | None = None
        self.username: str | None = None
        self.window: webview.Window | None = None

    # ---------------------------------------------------------------- auth
    def autoLogin(self) -> dict:
        if not self.store.exists():
            return {"firstRun": True}
        try:
            username, password = self.store.load()
            api = MercatorumAPI()
            api.login(username, password)
            self.api = api
            self.username = username
            return {
                "firstRun": False, "ok": True, "username": username,
                "courses": [self._course_to_dict(c) for c in api.list_courses()],
            }
        except AuthError as e:
            return {"firstRun": False, "ok": False, "error": f"Login fallito: {e}"}
        except Exception as e:
            log.exception("autoLogin failed")
            return {"firstRun": False, "ok": False, "error": str(e)}

    def login(self, username: str, password: str, remember: bool) -> dict:
        try:
            api = MercatorumAPI()
            api.login(username, password)
            if remember:
                self.store.save(username, password)
            self.api = api
            self.username = username
            return {
                "ok": True, "username": username,
                "courses": [self._course_to_dict(c) for c in api.list_courses()],
            }
        except AuthError as e:
            return {"ok": False, "error": f"Login fallito: {e}"}
        except Exception as e:
            log.exception("login failed")
            return {"ok": False, "error": str(e)}

    def forgetAccount(self) -> dict:
        self.store.reset()
        self.api = None
        self.username = None
        return {"ok": True}

    def logout(self) -> dict:
        self.api = None
        self.username = None
        return {"ok": True}

    # ------------------------------------------------------------- extract
    def extract(self, course_codes: list[str]) -> dict:
        if not self.api:
            return {"ok": False, "error": "Non autenticato."}
        threading.Thread(
            target=self._extract_worker, args=(list(course_codes),), daemon=True
        ).start()
        return {"ok": True}

    def _extract_worker(self, course_codes: list[str]) -> None:
        assert self.api is not None
        course_map = {c.code: c for c in self.api.list_courses()}
        for code in course_codes:
            course = course_map.get(code)
            if not course:
                self._emit({"kind": "course_error", "course_code": code,
                            "message": f"Corso {code} non trovato"})
                continue
            log.info("scanning tests for %s (%s)", code, course.name)
            self._emit({"kind": "course_start", "course_code": code,
                        "course_name": course.name})
            try:
                tests = self.api.get_course_tests(code)
            except Exception as e:
                log.exception("get_course_tests failed for %s", code)
                self._emit({"kind": "course_error", "course_code": code,
                            "message": f"Fetch test list fallito: {e}"})
                continue
            self._emit({"kind": "course_progress", "course_code": code,
                        "total": len(tests), "phase": "found tests"})

            # Group tests by module for the PDF.
            sections: list[QuizSection] = []
            for i, t in enumerate(tests, start=1):
                if t.module_title is None:
                    continue
                try:
                    raw = self.api.get_quiz_data(code, t)
                except Exception as e:
                    log.warning("getLessonTest failed for lp_item_id=%s: %s",
                                t.lp_item_id, e)
                    self._emit({"kind": "course_progress", "course_code": code,
                                "phase": f"[{i}/{len(tests)}] test {t.lp_item_id} skip"})
                    continue
                qa = extract_quiz_qa(raw)
                if not qa:
                    log.info("no inline correct flags for lp_item_id=%s — trying brute-force",
                             t.lp_item_id)
                    qa = brute_force_quiz(self.api, code, t)
                if qa:
                    sections.append(QuizSection(
                        module_number=t.module_number,
                        module_title=t.module_title,
                        qa=qa,
                    ))
                self._emit({"kind": "course_progress", "course_code": code,
                            "phase": f"[{i}/{len(tests)}] {len(qa)} Q&A"})

            # Render the PDF.
            out_path = OUTPUT / slugify(course.name) / "quiz_risposte.pdf"
            try:
                render_pdf(course.name, sections, out_path)
                total_qa = sum(len(s.qa) for s in sections)
                self._emit({"kind": "course_done", "course_code": code,
                            "course_name": course.name,
                            "modules": len(sections), "questions": total_qa,
                            "pdf": str(out_path)})
            except Exception as e:
                log.exception("PDF render failed for %s", code)
                self._emit({"kind": "course_error", "course_code": code,
                            "message": f"PDF: {e}"})
        self._emit({"kind": "all_done"})

    def _emit(self, evt: dict) -> None:
        if not self.window:
            return
        payload = json.dumps(evt)
        self.window.evaluate_js(f"window.notifyProgress({json.dumps(payload)})")

    def openOutputFolder(self) -> dict:
        OUTPUT.mkdir(parents=True, exist_ok=True)
        if sys.platform == "darwin":
            subprocess.run(["open", str(OUTPUT)], check=False)
        elif sys.platform.startswith("linux"):
            subprocess.run(["xdg-open", str(OUTPUT)], check=False)
        elif sys.platform == "win32":
            subprocess.run(["explorer", str(OUTPUT)], check=False)
        return {"ok": True}

    # ----------------------------------------------------------- helpers
    def _course_to_dict(self, c) -> dict:
        return {"code": c.code, "name": c.name, "progress": c.progress}


def main() -> int:
    log.info("Automercatorum Risposte Quiz")
    log.info("auth dir: %s", AUTH_DIR)
    log.info("output dir: %s", OUTPUT)
    js_api = JsApi()
    window = webview.create_window(
        "Automercatorum Risposte Quiz",
        str(UI_INDEX),
        js_api=js_api,
        width=960, height=720, min_size=(700, 500),
    )
    js_api.window = window
    webview.start()
    return 0


if __name__ == "__main__":
    sys.exit(main())

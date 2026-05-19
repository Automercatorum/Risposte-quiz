"""Unified REST client for Universitas Mercatorum LMS.

Combines the functionality of the three sister projects:
- Dispense (PDFs):  `get_course_pdfs(course_code) -> list[Pdf]`
- Video (MP4s):    `get_course_videos(course_code) -> list[Video]`
- Quiz (Q&A):      `get_course_tests(course_code) -> list[Test]`
                   `get_quiz_data(course_code, test) -> raw json`

All three share the same lp-folders → lessons → paragraphs traversal.
"""

from __future__ import annotations

import logging
from html import unescape
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

import requests

log = logging.getLogger(__name__)

SIGNIN_BASE = "https://signin-api.prod.multiversity.click"
LMS_BASE = "https://lms-api.prod.mercatorum.multiversity.click"

CLIENT_ID = 5
CLIENT_SECRET = "joySKkF8sldY0CTv3QvuIoCsKdKRpiZqEKJcfAsF"

BRUTE_FORCE_MAX_LP_ID = 200


class AuthError(Exception):
    pass


@dataclass
class Course:
    code: str
    name: str
    progress: float | None = None
    raw: dict = field(default_factory=dict, repr=False)


@dataclass
class Pdf:
    url: str
    module_number: int | None
    module_title: str | None


@dataclass
class Video:
    url: str
    module_number: int | None
    module_title: str | None
    paragraph_number: int | None
    paragraph_title: str | None
    duration_s: int | None = None


@dataclass
class Lesson:
    course_code: str
    lp_id: int
    folder_id: int | None
    folder_name: str | None
    title: str
    order: int | None
    progress: float | None
    raw: dict = field(default_factory=dict, repr=False)


@dataclass
class Paragraph:
    lp_item_id: int
    lesson_id: int | None
    title: str
    content_type: str
    duration_s: int | None
    max_time_allowed: int | None
    percentage: float
    total_time: int | None
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def is_complete(self) -> bool:
        return (self.percentage or 0) >= 100


@dataclass
class Test:
    lp_item_id: int
    lp_id: int
    test_id: int
    test_imported: int
    module_number: int | None
    module_title: str | None


@dataclass
class QuizQA:
    question: str
    correct_answers: list[str]
    all_answers: list[str] = field(default_factory=list)
    question_images: list[str] = field(default_factory=list)
    correct_answer_images: list[str] = field(default_factory=list)
    all_answer_images: list[list[str]] = field(default_factory=list)
    paragraph: str | None = None
    subtopic: str | None = None


class MercatorumAPI:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "User-Agent": "AutomercatorumRisposteQuiz/1.0",
        })
        self.access_token: str | None = None
        self.refresh_token: str | None = None
        self._credentials: tuple[str, str] | None = None

    # ------------------------------------------------------------------ auth
    def login(self, username: str, password: str) -> None:
        log.info("🔐 login request start")
        payload = {
            "username": username, "password": password,
            "grant_type": "password",
            "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
            "scope": "*",
        }
        log.debug("🌐 token request prepared")
        r = self.session.post(f"{SIGNIN_BASE}/oauth/token", json=payload, timeout=20)
        if r.status_code != 200:
            log.warning("🔐 login rejected: status=%s", r.status_code)
            raise AuthError(f"Login failed [{r.status_code}]")
        data = r.json()
        self.access_token = data.get("access_token")
        self.refresh_token = data.get("refresh_token")
        if not self.access_token:
            log.error("🔐 login response missing access token")
            raise AuthError("No access_token in response")
        self._credentials = (username, password)
        self.session.headers["Authorization"] = f"Bearer {self.access_token}"
        log.info("🔐 login request success")

    def _refresh_or_relogin(self) -> None:
        log.warning("🔄 session refresh requested")
        if self.refresh_token:
            payload = {
                "refresh_token": self.refresh_token, "grant_type": "refresh_token",
                "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET, "scope": "*",
            }
            r = self.session.post(f"{SIGNIN_BASE}/oauth/token", json=payload, timeout=20)
            if r.status_code == 200:
                data = r.json()
                self.access_token = data.get("access_token") or self.access_token
                self.refresh_token = data.get("refresh_token") or self.refresh_token
                self.session.headers["Authorization"] = f"Bearer {self.access_token}"
                log.info("🔄 session refresh success")
                return
        if self._credentials:
            log.warning("🔄 refresh failed; relogin fallback")
            self.login(*self._credentials)
        else:
            log.error("🔄 session refresh unavailable: no credentials")
            raise AuthError("Session expired and no credentials available.")

    # ----------------------------------------------------------- transport
    def _get(self, path: str) -> Any:
        log.debug("🌐 GET request start: kind=%s", _path_kind(path))
        url = f"{LMS_BASE}/{path.lstrip('/')}"
        r = self.session.get(url, timeout=30)
        if r.status_code == 401:
            log.warning("🌐 GET got 401; refreshing session")
            self._refresh_or_relogin()
            r = self.session.get(url, timeout=30)
        log.debug("🌐 GET response: kind=%s status=%s", _path_kind(path), r.status_code)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, data: dict | None = None) -> Any:
        log.debug("🌐 POST request start: kind=%s payload_keys=%s",
                  _path_kind(path), sorted((data or {}).keys()))
        url = f"{LMS_BASE}/{path.lstrip('/')}"
        r = self.session.post(url, json=data or {}, timeout=30)
        if r.status_code == 401:
            log.warning("🌐 POST got 401; refreshing session")
            self._refresh_or_relogin()
            r = self.session.post(url, json=data or {}, timeout=30)
        log.debug("🌐 POST response: kind=%s status=%s", _path_kind(path), r.status_code)
        r.raise_for_status()
        return r.json()

    # ---------------------------------------------------------------- data
    def list_courses(self) -> list[Course]:
        log.info("📚 courses list request")
        data = self._get("student/video-lessons/getCourses")
        items = _unwrap_list(data)
        out: list[Course] = []
        for it in items:
            code = str(it.get("course_code") or it.get("code") or it.get("id") or "").strip()
            name = _clean_text(it.get("course_name") or it.get("name") or it.get("title") or code)
            progress = it.get("progress") or it.get("percentage") or it.get("perc")
            if code:
                out.append(Course(code=code, name=name, progress=progress, raw=it))
        log.info("📚 courses list parsed: count=%d", len(out))
        return out

    def _walk_lessons(self, course_code: str) -> list[tuple[int, int | None, str, Any]]:
        """Return (lp_id, display_order, lesson_name, paragraphs_data) for every
        lesson in the course. Falls back to brute-force when lp-folders empty."""
        log.debug("🧭 lesson walk start")
        folders_resp = self._get(f"student/course/{course_code}/video-lessons/lp-folders")
        folders = _unwrap_list(folders_resp)
        log.debug("🧭 lesson folders parsed: count=%d", len(folders))

        def fetch_folder(folder: dict) -> list[dict]:
            folder_id = folder.get("id_folder") or folder.get("id")
            if folder_id is None:
                return []
            try:
                data = self._get(f"student/course/{course_code}/video-lessons/{folder_id}")
                return _unwrap_list(data)
            except Exception as e:
                log.warning("⚠️ lesson folder fetch failed: %s", e)
                return []

        lessons: list[tuple[int, int | None, str]] = []
        if folders:
            with ThreadPoolExecutor(max_workers=8) as ex:
                folder_lessons = list(ex.map(fetch_folder, folders))
            for lessons_in_folder in folder_lessons:
                for lesson in lessons_in_folder:
                    lp_id = lesson.get("lp_id") or lesson.get("id")
                    if lp_id is None:
                        continue
                    lessons.append((
                        int(lp_id),
                        lesson.get("display_order"),
                        _clean_text(lesson.get("name") or lesson.get("title") or f"lp_{lp_id}"),
                    ))

        if not lessons:
            log.info("🧭 lesson folders empty; fallback scan enabled")
            lessons = [(i, i, f"lp_{i}") for i in range(1, BRUTE_FORCE_MAX_LP_ID + 1)]

        def fetch_paragraphs(item):
            lp_id, _, _ = item
            try:
                data = self._get(
                    f"student/course/{course_code}/video-lesson/{lp_id}/paragraphs/{lp_id}"
                )
                return item, data
            except Exception:
                return item, None

        with ThreadPoolExecutor(max_workers=8) as ex:
            results = list(ex.map(fetch_paragraphs, lessons))

        out = []
        for (lp_id, display_order, lesson_name), data in results:
            if not data:
                continue
            real_title = _find_lesson_title(data)
            title = real_title or lesson_name
            out.append((lp_id, display_order, title, data))
        log.info("🧭 lesson walk done: lessons=%d", len(out))
        return out

    def get_course_pdfs(self, course_code: str) -> list[Pdf]:
        log.info("📄 pdf discovery start")
        pdfs: list[Pdf] = []
        seen: set[str] = set()
        for lp_id, display_order, title, data in self._walk_lessons(course_code):
            for url in _extract_book_urls(data):
                if url in seen:
                    continue
                seen.add(url)
                pdfs.append(Pdf(url=url, module_number=display_order, module_title=title))
        log.info("📄 pdf discovery done: count=%d", len(pdfs))
        return pdfs

    def get_course_videos(self, course_code: str) -> list[Video]:
        log.info("🎬 video discovery start")
        videos: list[Video] = []
        seen: set[str] = set()
        for lp_id, display_order, title, data in self._walk_lessons(course_code):
            for item in _extract_video_items(data):
                url = item["url"]
                if url in seen:
                    continue
                seen.add(url)
                videos.append(Video(
                    url=url,
                    module_number=display_order,
                    module_title=title,
                    paragraph_number=item.get("paragraph_number"),
                    paragraph_title=item.get("paragraph_title"),
                    duration_s=item.get("duration_s"),
                ))
        log.info("🎬 video discovery done: count=%d", len(videos))
        return videos

    def list_lessons(self, course_code: str) -> list[Lesson]:
        """List lessons for the watch tool.

        Mirrors `_walk_lessons` discovery: when the lp-folders payload is empty
        or returns no usable lessons, fall back to a brute-force lp_id probe
        — without it, courses that don't expose a clean folder hierarchy show
        no lessons at all (only Analisi 2 had one in the original code).
        """
        log.info("👀 watch lesson listing start")
        folders_resp = self._get(f"student/course/{course_code}/video-lessons/lp-folders")
        folders = _unwrap_list(folders_resp)
        log.debug("👀 watch folders parsed: count=%d", len(folders))

        def fetch_folder(folder: dict) -> tuple[dict, list[dict]]:
            folder_id = (folder.get("id_folder") or folder.get("id")
                         or folder.get("lp_id") or folder.get("folder_id"))
            if folder_id is None:
                return folder, []
            try:
                data = self._get(f"student/course/{course_code}/video-lessons/{folder_id}")
                return folder, _unwrap_list(data)
            except Exception as e:
                log.warning("⚠️ watch folder fetch failed: %s", e)
                return folder, []

        lessons: list[Lesson] = []
        if folders:
            with ThreadPoolExecutor(max_workers=8) as ex:
                results = list(ex.map(fetch_folder, [f for f in folders if isinstance(f, dict)]))
            for folder, folder_lessons in results:
                folder_id = _first_int(folder, ("id_folder", "id", "lp_id", "folder_id"))
                folder_name = _first_str(folder, ("folder_name", "name", "title"))
                for lesson in folder_lessons:
                    lp_id = _first_int(lesson, ("lp_id", "id"))
                    if lp_id is None:
                        continue
                    title = _first_str(lesson, ("name", "title", "lp_name")) or f"Lezione {lp_id}"
                    progress = lesson.get("progress") or lesson.get("percentage") or lesson.get("perc")
                    lessons.append(Lesson(
                        course_code=course_code,
                        lp_id=lp_id,
                        folder_id=folder_id,
                        folder_name=folder_name,
                        title=title,
                        order=_first_int(lesson, ("display_order", "sort", "order", "lp_order")),
                        progress=progress,
                        raw=lesson,
                    ))

        if not lessons:
            log.info("👀 watch lesson listing fallback: brute-force probe")
            lessons = self._brute_force_lessons(course_code)

        log.info("👀 watch lesson listing done: count=%d", len(lessons))
        return lessons

    def _brute_force_lessons(self, course_code: str) -> list[Lesson]:
        """Probe lp_id 1..N concurrently for paragraph endpoints that return
        data, and synthesize Lesson objects for them. Used as a fallback when
        the LMS folder hierarchy is missing or unparseable for a course."""

        def probe(lp_id: int) -> Lesson | None:
            try:
                data = self._get(
                    f"student/course/{course_code}/video-lesson/{lp_id}/paragraphs/{lp_id}"
                )
            except Exception:
                return None
            items = _unwrap_list(data)
            if not items:
                return None
            video_items = [it for it in items
                           if (it.get("contentType") or it.get("content_type")) == "video"]
            progress = None
            if video_items:
                pcts = [float(it.get("percentage") or 0) for it in video_items]
                progress = sum(pcts) / len(pcts) if pcts else None
            title = _find_lesson_title(data) or f"Lezione {lp_id}"
            return Lesson(
                course_code=course_code,
                lp_id=lp_id,
                folder_id=None,
                folder_name=None,
                title=title,
                order=lp_id,
                progress=progress,
                raw={},
            )

        found: list[Lesson] = []
        with ThreadPoolExecutor(max_workers=8) as ex:
            for lesson in ex.map(probe, range(1, BRUTE_FORCE_MAX_LP_ID + 1)):
                if lesson is not None:
                    found.append(lesson)
        return found

    def get_lesson_paragraphs(self, course_code: str, lp_id: int) -> list[Paragraph]:
        log.debug("👀 paragraph listing start")
        data = self._get(
            f"student/course/{course_code}/video-lesson/{lp_id}/paragraphs/{lp_id}"
        )
        paragraphs: list[Paragraph] = []
        for item in _unwrap_list(data):
            lp_item_id = _first_int(item, ("lp_item_id", "id"))
            if lp_item_id is None:
                continue
            paragraphs.append(Paragraph(
                lp_item_id=lp_item_id,
                lesson_id=_first_int(item, ("lessonId", "lesson_id")),
                title=_first_str(item, ("title", "name")) or f"Item {lp_item_id}",
                content_type=_first_str(item, ("contentType", "content_type")) or "",
                duration_s=_first_int(item, ("duration_s", "duration")),
                max_time_allowed=_first_int(item, ("max_time_allowed",)),
                percentage=float(item.get("percentage") or 0),
                total_time=_first_int(item, ("total_time", "totalTime")),
                raw=item,
            ))
        log.debug("👀 paragraph listing done: count=%d", len(paragraphs))
        return paragraphs

    def get_course_tests(self, course_code: str) -> list[Test]:
        log.info("📝 quiz test discovery start")
        tests: list[Test] = []
        for lp_id, display_order, title, data in self._walk_lessons(course_code):
            for item in _walk_items(data):
                if not isinstance(item, dict):
                    continue
                if item.get("contentType") == "test" and not item.get("testEmpty"):
                    test_lp_item_id = item.get("lp_item_id") or item.get("id")
                    if test_lp_item_id is None:
                        continue
                    test_id = item.get("testId") or item.get("test_id") or item.get("id")
                    if test_id is None:
                        log.warning("📝 quiz test skipped: missing test id")
                        continue
                    test_imported = int(item.get("testImported") or 0)
                    tests.append(Test(
                        lp_item_id=int(test_lp_item_id),
                        lp_id=int(lp_id),
                        test_id=int(test_id),
                        test_imported=test_imported,
                        module_number=display_order,
                        module_title=title,
                    ))
        log.info("📝 quiz test discovery done: count=%d", len(tests))
        return tests

    def get_quiz_data(self, course_code: str, test: Test) -> Any:
        log.debug("📝 quiz source request")
        body = {
            "course_code": course_code,
            "lp_item_id": test.lp_item_id,
            "lp_id": test.lp_id,
            "testId": test.test_id,
            "testImported": test.test_imported,
        }
        return self._post(
            f"student/course/{course_code}/video-lessons/test/source", body
        )


# ---------------------------------------------------------------- helpers
def _unwrap_list(data: Any) -> list[dict]:
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in ("data", "courses", "items", "result", "results"):
            v = data.get(key)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
    return []


def _walk_items(node: Any):
    if isinstance(node, dict):
        if "contentType" in node:
            yield node
        for v in node.values():
            yield from _walk_items(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk_items(v)


def _extract_book_urls(node: Any) -> list[str]:
    out: list[str] = []
    if isinstance(node, dict):
        bu = node.get("bookUrl") or node.get("book_url")
        if isinstance(bu, str) and bu.startswith("http"):
            out.append(bu)
        for v in node.values():
            out.extend(_extract_book_urls(v))
    elif isinstance(node, list):
        for v in node:
            out.extend(_extract_book_urls(v))
    return out


def _extract_video_items(node: Any) -> list[dict]:
    out: list[dict] = []
    if isinstance(node, dict):
        url = node.get("videoUrl")
        if isinstance(url, str) and url.startswith("http"):
            out.append({
                "url": url,
                "paragraph_number": node.get("paragNumber"),
                "paragraph_title": node.get("title"),
                "duration_s": node.get("duration_s"),
            })
        for v in node.values():
            out.extend(_extract_video_items(v))
    elif isinstance(node, list):
        for v in node:
            out.extend(_extract_video_items(v))
    return out


def _find_lesson_title(node: Any) -> str | None:
    if isinstance(node, dict):
        if node.get("contentType") == "lesson":
            title = node.get("title") or node.get("name")
            if isinstance(title, str) and title.strip():
                return _clean_text(title)
        for v in node.values():
            r = _find_lesson_title(v)
            if r:
                return r
    elif isinstance(node, list):
        for v in node:
            r = _find_lesson_title(v)
            if r:
                return r
    return None


def _first_int(d: dict, keys: tuple[str, ...]) -> int | None:
    for key in keys:
        value = d.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return None


def _first_str(d: dict, keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = d.get(key)
        if isinstance(value, str) and value.strip():
            return _clean_text(value)
    return None


def _clean_text(value: Any) -> str:
    return unescape(str(value)).strip()


def _path_kind(path: str) -> str:
    if "getCourses" in path:
        return "courses"
    if "lp-folders" in path:
        return "lesson-folders"
    if "paragraphs" in path:
        return "paragraphs"
    if "test/source" in path:
        return "quiz-source"
    if "video-lessons/test" in path:
        return "quiz"
    if "video-lessons" in path:
        return "lessons"
    return "lms"

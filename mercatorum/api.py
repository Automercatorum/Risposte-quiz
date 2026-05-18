"""REST client for Universitas Mercatorum LMS — practice quiz answer extraction.

Builds on the same patterns as the dispense/video sister projects:
- Auth via signin-api OAuth2 password grant
- LMS API at lms-api.prod.mercatorum.multiversity.click
- Per-lesson paragraph traversal to find `contentType: "test"` items
- `getLessonTest` (POST student/course/{cc}/video-lessons/test/source) for the
  actual quiz payload — expected to include answer options + correct flags.
"""

from __future__ import annotations

import logging
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
class Test:
    """A practice quiz attached to a lesson."""
    lp_item_id: int
    lp_id: int
    test_id: int                       # lessonId — primary key for the test
    test_imported: int                 # `testImported` flag (0/1)
    module_number: int | None
    module_title: str | None


@dataclass
class QuizQA:
    """A single question with its correct answer(s)."""
    question: str
    correct_answers: list[str]
    all_answers: list[str] = field(default_factory=list)
    paragraph: str | None = None
    subtopic: str | None = None


class MercatorumAPI:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "User-Agent": "MercatorumQuizExtractor/1.0",
        })
        self.access_token: str | None = None
        self.refresh_token: str | None = None
        self._credentials: tuple[str, str] | None = None

    # ------------------------------------------------------------------ auth
    def login(self, username: str, password: str) -> None:
        payload = {
            "username": username, "password": password,
            "grant_type": "password",
            "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
            "scope": "*",
        }
        log.info("login user=%r", username)
        r = self.session.post(f"{SIGNIN_BASE}/oauth/token", json=payload, timeout=20)
        if r.status_code != 200:
            raise AuthError(f"Login failed [{r.status_code}]: {r.text[:200]}")
        data = r.json()
        self.access_token = data.get("access_token")
        self.refresh_token = data.get("refresh_token")
        if not self.access_token:
            raise AuthError(f"No access_token in response: {data}")
        self._credentials = (username, password)
        self.session.headers["Authorization"] = f"Bearer {self.access_token}"

    def _refresh_or_relogin(self) -> None:
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
                return
        if self._credentials:
            self.login(*self._credentials)
        else:
            raise AuthError("Session expired and no credentials available.")

    # ----------------------------------------------------------- transport
    def _get(self, path: str) -> Any:
        url = f"{LMS_BASE}/{path.lstrip('/')}"
        r = self.session.get(url, timeout=30)
        if r.status_code == 401:
            self._refresh_or_relogin()
            r = self.session.get(url, timeout=30)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, data: dict | None = None) -> Any:
        url = f"{LMS_BASE}/{path.lstrip('/')}"
        r = self.session.post(url, json=data or {}, timeout=30)
        if r.status_code == 401:
            self._refresh_or_relogin()
            r = self.session.post(url, json=data or {}, timeout=30)
        r.raise_for_status()
        return r.json()

    # ---------------------------------------------------------------- data
    def list_courses(self) -> list[Course]:
        data = self._get("student/video-lessons/getCourses")
        items = _unwrap_list(data)
        out: list[Course] = []
        for it in items:
            code = str(it.get("course_code") or it.get("code") or it.get("id") or "").strip()
            name = (it.get("course_name") or it.get("name") or it.get("title") or code).strip()
            progress = it.get("progress") or it.get("percentage") or it.get("perc")
            if code:
                out.append(Course(code=code, name=name, progress=progress, raw=it))
        return out

    def get_course_tests(self, course_code: str) -> list[Test]:
        """Walk every lesson in the course, return all test items."""
        folders_resp = self._get(f"student/course/{course_code}/video-lessons/lp-folders")
        folders = _unwrap_list(folders_resp)

        def fetch_folder(folder: dict) -> list[dict]:
            folder_id = folder.get("id_folder") or folder.get("id")
            if folder_id is None:
                return []
            try:
                data = self._get(f"student/course/{course_code}/video-lessons/{folder_id}")
                return _unwrap_list(data)
            except Exception as e:
                log.warning("folder %s fetch failed: %s", folder_id, e)
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
                        lesson.get("name") or lesson.get("title") or f"lp_{lp_id}",
                    ))

        if not lessons:
            log.info("lp-folders empty for %s — brute-forcing lp_ids 1..%d",
                     course_code, BRUTE_FORCE_MAX_LP_ID)
            lessons = [(i, i, f"lp_{i}") for i in range(1, BRUTE_FORCE_MAX_LP_ID + 1)]

        def fetch_lesson(item):
            lp_id, _, _ = item
            try:
                data = self._get(
                    f"student/course/{course_code}/video-lesson/{lp_id}/paragraphs/{lp_id}"
                )
                return item, data
            except Exception:
                return item, None

        with ThreadPoolExecutor(max_workers=8) as ex:
            results = list(ex.map(fetch_lesson, lessons))

        tests: list[Test] = []
        for (lp_id, display_order, lesson_name), data in results:
            if not data:
                continue
            real_title = _find_lesson_title(data)
            title = real_title or lesson_name
            for item in _walk_items(data):
                if not isinstance(item, dict):
                    continue
                if item.get("contentType") == "test" and not item.get("testEmpty"):
                    test_lp_item_id = item.get("lp_item_id") or item.get("id")
                    if test_lp_item_id is None:
                        continue
                    test_id = item.get("lessonId") or item.get("test_id") or item.get("id") or 0
                    test_imported = int(item.get("testImported") or 0)
                    tests.append(Test(
                        lp_item_id=int(test_lp_item_id),
                        lp_id=int(lp_id),
                        test_id=int(test_id),
                        test_imported=test_imported,
                        module_number=display_order,
                        module_title=title,
                    ))
        return tests

    def get_quiz_data(self, course_code: str, test: Test) -> Any:
        """Fetch the raw test payload.

        Body shape inferred from server validation errors: requires
        `testId` (= lessonId), `lp_id`, `testImported`. We also send
        `course_code` and `lp_item_id` for completeness.
        """
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
    """Yield every dict that could be a paragraph item (has contentType)."""
    if isinstance(node, dict):
        if "contentType" in node:
            yield node
        for v in node.values():
            yield from _walk_items(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk_items(v)


def _find_lesson_title(node: Any) -> str | None:
    if isinstance(node, dict):
        if node.get("contentType") == "lesson":
            title = node.get("title") or node.get("name")
            if isinstance(title, str) and title.strip():
                return title.strip()
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

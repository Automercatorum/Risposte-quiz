#!/usr/bin/env python3
"""CLI alternative to the GUI app.

Usage:
    python extract.py --list                    # show your subjects
    python extract.py <COURSE_CODE> [...]       # extract Q&A for one or more
    python extract.py --all                     # extract for every subject
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

from mercatorum.api import AuthError, MercatorumAPI
from mercatorum.creds_store import CredentialsStore
from mercatorum.quiz_extractor import (
    QuizSection, brute_force_quiz, extract_quiz_qa, render_pdf, slugify,
)

ROOT = Path(__file__).resolve().parent
AUTH_DIR = ROOT / ".auth"
OUTPUT = ROOT / "output"


def authenticate() -> MercatorumAPI:
    store = CredentialsStore(AUTH_DIR)
    if store.exists():
        username, password = store.load()
    else:
        username = input("Username/matricola: ").strip()
        password = getpass.getpass("Password: ")
        if input("Salvare credenziali? [y/N] ").strip().lower() == "y":
            store.save(username, password)
    api = MercatorumAPI()
    try:
        api.login(username, password)
    except AuthError as e:
        print(f"Login fallito: {e}", file=sys.stderr)
        sys.exit(1)
    return api


def extract_for_course(api, course) -> Path | None:
    tests = api.get_course_tests(course.code)
    print(f"  {len(tests)} quiz trovati")
    sections: list[QuizSection] = []
    for i, t in enumerate(tests, start=1):
        if t.module_title is None:
            continue
        try:
            raw = api.get_quiz_data(course.code, t)
        except Exception as e:
            print(f"    [{i}/{len(tests)}] lp_item_id={t.lp_item_id}: SKIP ({e})")
            continue
        qa = extract_quiz_qa(raw) or brute_force_quiz(api, course.code, t)
        if qa:
            sections.append(QuizSection(
                module_number=t.module_number,
                module_title=t.module_title,
                qa=qa,
            ))
        print(f"    [{i}/{len(tests)}] {len(qa)} Q&A")

    out_path = OUTPUT / slugify(course.name) / "quiz_risposte.pdf"
    render_pdf(course.name, sections, out_path)
    total_qa = sum(len(s.qa) for s in sections)
    print(f"  ✓ {len(sections)} moduli, {total_qa} domande → {out_path}")
    return out_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Mercatorum quiz answer extractor (CLI).")
    parser.add_argument("codes", nargs="*", help="Course codes")
    parser.add_argument("--list", action="store_true", help="Only list subjects")
    parser.add_argument("--all", action="store_true", help="Extract for every subject")
    args = parser.parse_args()

    api = authenticate()
    courses = api.list_courses()

    if args.list:
        for c in courses:
            prog = f" ({c.progress:.0f}%)" if c.progress is not None else ""
            print(f"  {c.code}  {c.name}{prog}")
        return 0

    if args.all:
        targets = courses
    elif args.codes:
        by_code = {c.code: c for c in courses}
        targets = [by_code[code] for code in args.codes if code in by_code]
        for code in args.codes:
            if code not in by_code:
                print(f"  ! Codice non trovato: {code}", file=sys.stderr)
    else:
        parser.print_help()
        return 1

    for course in targets:
        print(f"\n→ {course.name} ({course.code})")
        try:
            extract_for_course(api, course)
        except Exception as e:
            print(f"  ! Errore: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

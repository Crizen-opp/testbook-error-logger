"""
Error Logger - Local Server
===========================
Receives scraped data from the Chrome extension, uses a local Ollama model to
structure and categorize it, stores it in SQLite, and serves a local web
dashboard with flashcard-based spaced repetition review.

Run:
    cd server
    pip install -r requirements.txt
    python app.py

Optional environment variables (see README for details):
    OLLAMA_URL      default http://localhost:11434
    OLLAMA_MODEL    default deepseek-v3.1:671b-cloud
    OLLAMA_TIMEOUT  default 180 (seconds per question)

Then open http://localhost:8787/
"""
import os
import sys
import io
import json
import sqlite3
import hashlib
import random

# Force UTF-8 stdout/stderr on Windows console (handles emoji in print statements)
if sys.stdout and hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if sys.stderr and hasattr(sys.stderr, "buffer"):
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

IST_OFFSET = timedelta(hours=5, minutes=30)


def ist_midnight_utc(days_from_now: int) -> datetime:
    """UTC instant of midnight IST, `days_from_now` calendar days ahead (IST calendar).

    Cards are due by *date*, not exact time — two reviews logged at 11am and
    11pm on the same day with the same interval must both unlock at 12am IST
    on the due date, not at their original time-of-day.
    """
    due_date_ist = (datetime.utcnow() + IST_OFFSET + timedelta(days=days_from_now)).date()
    midnight_ist = datetime(due_date_ist.year, due_date_ist.month, due_date_ist.day)
    return midnight_ist - IST_OFFSET

import asyncio

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import uvicorn

# ---------- Config ----------
if getattr(sys, "frozen", False):
    # Running as PyInstaller bundle
    _bundle_dir = Path(sys._MEIPASS)
    BASE_DIR = _bundle_dir
    # Prefer an EXTERNAL webapp/ folder sitting next to the exe if it has an
    # index.html — this lets dashboard (UI) edits take effect with just a browser
    # refresh, no PyInstaller rebuild. Falls back to the bundled copy otherwise,
    # so a lone exe still works exactly as before.
    _external_webapp = Path(sys.executable).resolve().parent / "webapp"
    WEBAPP_DIR = _external_webapp if (_external_webapp / "index.html").exists() else (_bundle_dir / "webapp")
    _local_source_db = Path(sys.executable).resolve().parent / "errors.db"
else:
    BASE_DIR = Path(__file__).resolve().parent
    WEBAPP_DIR = BASE_DIR.parent / "webapp"
    _local_source_db = BASE_DIR / "dist" / "errors.db"

# DB location: ALWAYS a fixed, machine-local path — independent of where the exe/script
# happens to sit (Desktop shortcut, dist/, a USB stick, …). Copying or pinning the exe
# must never silently switch databases. Deliberately NOT under Desktop/Documents: OneDrive
# syncs those and can resurrect a stale copy over the real one (bit us on 2026-07-10, twice
# — first via a resurrected Desktop errors.db, then via the exe itself being copied there).
# ERROR_LOGGER_DB_PATH is the current name; TESTBOOK_DB_PATH is still honoured so
# installs predating the rename keep working.
_env_db = os.environ.get("ERROR_LOGGER_DB_PATH") or os.environ.get("TESTBOOK_DB_PATH")
if _env_db:
    DB_PATH = Path(_env_db).expanduser().resolve()
else:
    _local_appdata = os.environ.get("LOCALAPPDATA")
    _app_data_root = Path(_local_appdata) if _local_appdata else Path.home() / ".local" / "share"
    DB_PATH = _app_data_root / "ErrorLogger" / "errors.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# One-time bootstrap: if the canonical DB doesn't exist yet, seed it (db + images) from
# the best available source — next to this exe/script first (freshest), then the two
# pre-rename locations (%LOCALAPPDATA%, then the older %APPDATA%/Roaming one).
if not DB_PATH.exists():
    import shutil
    _legacy_local = Path(os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))) / "TestbookErrorLogger" / "errors.db"
    _old_db = Path(os.environ.get("APPDATA", os.path.expanduser("~"))) / "TestbookErrorLogger" / "errors.db"
    for _src in (_local_source_db, _legacy_local, _old_db):
        if _src.exists() and _src != DB_PATH:
            shutil.copy2(str(_src), str(DB_PATH))
            _src_images = _src.parent / "errors_images"
            _dst_images = DB_PATH.parent / "errors_images"
            if _src_images.exists() and not _dst_images.exists():
                shutil.copytree(str(_src_images), str(_dst_images))
            print(f"📦 Migrated existing DB from {_src} → {DB_PATH}")
            break
PORT = 8787

# Ollama config - reads from env, sane defaults
# Default is DeepSeek V3.1 via Ollama Cloud (free preview, requires `ollama signin`)
# Override with OLLAMA_MODEL=qwen2.5:7b-instruct for fully local mode
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b-instruct")
OLLAMA_TIMEOUT = int(os.environ.get("OLLAMA_TIMEOUT", "180"))  # cloud 671B can be slower on first token

# Check Ollama availability at startup (non-fatal - fallback kicks in)
def _check_ollama():
    try:
        r = httpx.get(f"{OLLAMA_URL}/api/tags", timeout=5)
        models = [m["name"] for m in r.json().get("models", [])]
        is_cloud = OLLAMA_MODEL.endswith("-cloud") or ":cloud" in OLLAMA_MODEL

        if not is_cloud:
            if not any(OLLAMA_MODEL.split(":")[0] in m for m in models):
                print(f"⚠️  Ollama is running but '{OLLAMA_MODEL}' not pulled.")
                print(f"   Run: ollama pull {OLLAMA_MODEL}")
                return False
        else:
            print(f"☁️  Using Ollama Cloud model. If calls fail with 401/403, run: ollama signin")

        print(f"✅ Ollama ready at {OLLAMA_URL} with model {OLLAMA_MODEL}")
        return True
    except Exception:
        return False


def _ensure_ollama():
    """Start ollama serve in the background if it isn't already running."""
    import subprocess, time, shutil

    if _check_ollama():
        return True

    if not shutil.which("ollama"):
        print("⚠️  Ollama not installed. Install from https://ollama.com and run again.")
        return False

    print("🚀 Starting Ollama in the background...")
    try:
        subprocess.Popen(
            ["ollama", "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            # Detach from this process so it survives if the terminal closes
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except Exception as e:
        print(f"⚠️  Could not start Ollama: {e}")
        return False

    # Wait up to 15 s for the server to come up
    for i in range(15):
        time.sleep(1)
        if _check_ollama():
            return True
        if i == 4:
            print("   Still waiting for Ollama to start...")

    print("⚠️  Ollama didn't respond after 15 s — ingestion will use heuristic fallback.")
    return False


OLLAMA_READY = _ensure_ollama()

# ---------- Database ----------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS errors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            hash TEXT UNIQUE NOT NULL,
            question_text TEXT NOT NULL,
            options_json TEXT,
            your_answer TEXT,
            correct_answer TEXT,
            solution_text TEXT,
            subject TEXT,
            topic TEXT,
            subtopic TEXT,
            concept_tags_json TEXT,
            mistake_type TEXT,
            difficulty TEXT,
            test_title TEXT,
            test_url TEXT,
            captured_at TEXT,
            session_type TEXT DEFAULT 'mock',
            platform TEXT DEFAULT 'testbook',
            flagged INTEGER DEFAULT 0,
            notes_json TEXT,
            -- SM-2 spaced repetition fields
            sr_interval INTEGER DEFAULT 0,
            sr_ease REAL DEFAULT 2.5,
            sr_reps INTEGER DEFAULT 0,
            sr_due_at TEXT,
            sr_last_reviewed TEXT,
            sr_total_reviews INTEGER DEFAULT 0,
            sr_wrong_reviews INTEGER DEFAULT 0,
            mastered INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_due ON errors(sr_due_at);
        CREATE INDEX IF NOT EXISTS idx_subject ON errors(subject);
        CREATE INDEX IF NOT EXISTS idx_topic ON errors(topic);

        CREATE TABLE IF NOT EXISTS review_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            error_id INTEGER NOT NULL,
            quality INTEGER NOT NULL,  -- 0=wrong, 1=hard, 2=good, 3=easy
            reviewed_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (error_id) REFERENCES errors(id) ON DELETE CASCADE
        );
        """)

init_db()


def _migrate_db():
    """Add new columns to existing DBs that were created before these fields existed."""
    new_cols = [
        "ALTER TABLE errors ADD COLUMN session_type TEXT DEFAULT 'mock'",
        "ALTER TABLE errors ADD COLUMN platform TEXT DEFAULT 'testbook'",
        "ALTER TABLE errors ADD COLUMN flagged INTEGER DEFAULT 0",
        "ALTER TABLE errors ADD COLUMN notes_json TEXT",
        "ALTER TABLE errors ADD COLUMN question_images_json TEXT",
        "ALTER TABLE errors ADD COLUMN option_images_json TEXT",
        "ALTER TABLE errors ADD COLUMN solution_images_json TEXT",
        "ALTER TABLE errors ADD COLUMN source_url TEXT",
    ]
    with db() as conn:
        for sql in new_cols:
            try:
                conn.execute(sql)
            except Exception:
                pass  # column already exists
        # Create indexes for new columns only after columns are guaranteed to exist
        for idx_sql in [
            "CREATE INDEX IF NOT EXISTS idx_session ON errors(session_type)",
            "CREATE INDEX IF NOT EXISTS idx_flagged ON errors(flagged)",
        ]:
            try:
                conn.execute(idx_sql)
            except Exception:
                pass


_migrate_db()


def _normalize_due_to_ist_midnight():
    """One-time fix: snap every existing sr_due_at to midnight IST of its own date.

    Cards scheduled before ist_midnight_utc() existed kept the *time-of-day* of
    when they were reviewed (e.g. due 13:31 UTC = 7pm IST). That made them unlock
    at scattered times — 11am, noon, 4pm — instead of all at 12am IST on their
    due date. This rewrites those legacy rows so a card due on an IST date becomes
    available at the start of that IST day. Idempotent: rows already at midnight
    IST (18:30:00 UTC) are left untouched.
    """
    with db() as conn:
        rows = conn.execute(
            "SELECT id, sr_due_at FROM errors WHERE sr_due_at IS NOT NULL"
        ).fetchall()
        fixed = 0
        for r in rows:
            try:
                due = datetime.fromisoformat(r["sr_due_at"])
            except (ValueError, TypeError):
                continue
            due_date_ist = (due + IST_OFFSET).date()
            midnight = datetime(due_date_ist.year, due_date_ist.month, due_date_ist.day) - IST_OFFSET
            new_val = midnight.isoformat()
            if new_val != r["sr_due_at"]:
                conn.execute("UPDATE errors SET sr_due_at = ? WHERE id = ?", (new_val, r["id"]))
                fixed += 1
        if fixed:
            print(f"[migrate] normalized {fixed} due dates to midnight IST")


_normalize_due_to_ist_midnight()


# ---------- Subject / topic weighting (SSC PYQ weightage) ----------
# Weights bias which DUE card surfaces next in Review: a higher weight makes a
# card more likely to be picked (weighted-random, so no subject ever starves).
# GA subjects are seeded from the user's CGL-2025 PYQ trend report (History
# ~18%, Polity ~14.5%, Geography ~11.3% …); Quant/Reasoning/English stay neutral
# at 1.0 (separate exam sections, not GA). Topic overrides let Modern History
# outrank Ancient/Medieval. Everything is editable via /api/weights.
_SUBJECT_WEIGHT_SEED = [
    # subject, weight, is_ga, pyq_share%
    ("History", 3.0, 1, 18.0),
    ("Polity", 2.4, 1, 14.5),
    ("Geography", 1.9, 1, 11.3),
    ("General Awareness", 1.8, 1, 11.2),
    ("Art & Culture", 1.6, 1, 8.5),
    ("Sports", 1.5, 1, 7.9),
    ("Economics", 1.3, 1, 7.8),
    ("Economy", 1.3, 1, 7.8),
    ("Physics", 0.9, 1, 3.1),
    ("Biology", 0.9, 1, 2.8),
    ("Chemistry", 0.8, 1, 2.5),
    # non-GA sections stay neutral
    ("Quantitative Aptitude", 1.0, 0, 0.0),
    ("Reasoning", 1.0, 0, 0.0),
    ("English", 1.0, 0, 0.0),
    ("Computer Science", 1.0, 0, 0.0),
    ("Geometry", 1.0, 0, 0.0),
    ("Other", 1.0, 0, 0.0),
]
_TOPIC_WEIGHT_SEED = [
    # subject, topic, weight  (Modern History ranks highest within History)
    ("History", "Freedom Struggle", 3.6),
    ("History", "Modern India", 3.6),
    ("History", "Modern Indian History", 3.6),
    ("History", "Ancient India", 2.7),
    ("History", "Ancient Indian History", 2.7),
    ("History", "Mughal Empire", 2.6),
    ("History", "Medieval India", 2.5),
    ("History", "Medieval Indian History", 2.5),
    ("Polity", "Constitution & Articles", 2.8),
    ("Polity", "Parliament & Governance", 2.6),
    ("Geography", "Physical Geography", 2.1),
    ("Geography", "Rivers & Drainage", 2.0),
    ("Economics", "Indian Economy", 1.6),
    ("Economy", "Indian Economy", 1.6),
]


def _init_weights():
    with db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS subject_weights (
            subject TEXT PRIMARY KEY,
            weight REAL DEFAULT 1.0,
            is_ga INTEGER DEFAULT 0,
            pyq_share REAL DEFAULT 0
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS topic_weights (
            subject TEXT,
            topic TEXT,
            weight REAL DEFAULT 1.0,
            PRIMARY KEY (subject, topic)
        )""")
        # Seed once. INSERT OR IGNORE preserves the user's later edits.
        for s, w, ga, share in _SUBJECT_WEIGHT_SEED:
            conn.execute(
                "INSERT OR IGNORE INTO subject_weights (subject, weight, is_ga, pyq_share) VALUES (?,?,?,?)",
                (s, w, ga, share),
            )
        for s, t, w in _TOPIC_WEIGHT_SEED:
            conn.execute(
                "INSERT OR IGNORE INTO topic_weights (subject, topic, weight) VALUES (?,?,?)",
                (s, t, w),
            )


_init_weights()


def _load_weight_maps():
    """Return (subject_weight dict, topic_weight dict keyed by (subject, topic))."""
    with db() as conn:
        sw = {r["subject"]: r["weight"] for r in conn.execute("SELECT subject, weight FROM subject_weights")}
        tw = {(r["subject"], r["topic"]): r["weight"] for r in conn.execute("SELECT subject, topic, weight FROM topic_weights")}
    return sw, tw


def _resolve_weight(subject, topic, sw, tw):
    """Topic override wins over subject weight; default 1.0. Never returns 0."""
    w = tw.get((subject, topic))
    if w is None:
        w = sw.get(subject, 1.0)
    try:
        w = float(w)
    except (TypeError, ValueError):
        w = 1.0
    return max(w, 1e-4)


# ---------- Image localization ----------
# Question figures / image options / solution diagrams are scraped as CDN URLs
# (storage.googleapis.com/tb-img/...). We download each one next to the DB so
# flashcards work offline and survive CDN link rot; the dashboard serves them
# from /images/. Lives next to errors.db so the images travel with the data.
IMAGES_DIR = DB_PATH.parent / "errors_images"

_IMG_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp")


def _localize_image(url: str) -> str:
    """Download one image and return its local /images/ path.
    On any failure, return the original URL so the dashboard can still hotlink it."""
    if not isinstance(url, str) or not url.lower().startswith(("http://", "https://")):
        return url
    try:
        IMAGES_DIR.mkdir(parents=True, exist_ok=True)
        ext = os.path.splitext(url.split("?")[0])[1].lower()
        if ext not in _IMG_EXTS:
            ext = ".png"
        name = hashlib.sha1(url.encode()).hexdigest()[:16] + ext
        dest = IMAGES_DIR / name
        if not dest.exists():
            r = httpx.get(url, timeout=15, follow_redirects=True)
            r.raise_for_status()
            dest.write_bytes(r.content)
        return f"/images/{name}"
    except Exception as e:
        print(f"⚠️  Could not localize image {url[:80]}: {e}")
        return url


def _localize_images(urls) -> list:
    return [_localize_image(u) for u in (urls or []) if isinstance(u, str) and u]


def _localize_option_images(groups) -> list:
    """option_images is a list-of-lists aligned with options; keep the shape."""
    return [
        _localize_images(g if isinstance(g, list) else [g])
        for g in (groups or [])
    ]


def _parse_image_cols(d: dict) -> dict:
    """Replace *_images_json columns with parsed lists on an API response row."""
    d["question_images"] = json.loads(d.pop("question_images_json", None) or "[]")
    d["option_images"] = json.loads(d.pop("option_images_json", None) or "[]")
    d["solution_images"] = json.loads(d.pop("solution_images_json", None) or "[]")
    return d


# ---------- Ollama structuring ----------
# NOTE: We used to pass a JSON schema via `format`, but Ollama Cloud endpoints
# don't reliably accept schema objects - some return empty responses. Using
# `format: "json"` (simple mode) + a strong prompt works on both local and cloud.

STRUCTURE_PROMPT = """You are analyzing a question from an Indian competitive exam (SSC CGL, banking, IIT, NEET, etc.) scraped from an online learning platform (Testbook, PW/Physics Wallah, DPPS, Oliveboard, etc.). The student got this question WRONG or SKIPPED it.

Your task: extract clean structured data so it can be used for flashcard-based revision.

SUBJECT CLASSIFICATION RULES — pick the MOST SPECIFIC subject that fits:
- Quantitative Aptitude: math, arithmetic, percentages, profit/loss, time/work, algebra, geometry, number series
- Reasoning: logical reasoning, puzzles, coding-decoding, blood relations, syllogism, direction sense
- English: grammar, vocabulary, comprehension, idioms, fill in the blanks, sentence correction
- Economics: money, banking, RBI, inflation, GDP, fiscal policy, monetary policy, budget, taxes, markets, demand/supply, characteristics of money, types of economies, trade, currency — ANY economics/finance topic
- History: ancient/medieval/modern Indian history, world history, freedom struggle, dynasties, battles
- Geography: physical/human geography, rivers, mountains, climate, maps, agriculture, natural resources
- Polity: constitution, parliament, judiciary, president, governor, elections, government schemes, amendments
- Biology: life science, human body, plants, animals, diseases, ecology, genetics
- Chemistry: elements, compounds, reactions, periodic table, acids/bases
- Physics: mechanics, electricity, optics, thermodynamics, waves
- Computer Science: computers, internet, software, hardware, networking, OS
- General Awareness: current affairs or miscellaneous GK that does NOT fit any above category
IMPORTANT: Use "General Awareness" ONLY when no specific subject fits. "Characteristics of money", "functions of RBI", "types of banks" are ECONOMICS, not General Awareness.

Return a JSON object with EXACTLY these fields (no extra fields, no markdown fences, no commentary):

{
  "question_text": "clean question text, no 'Your Answer:' or 'Correct Answer:' labels inside",
  "options": ["A) ...", "B) ...", "C) ...", "D) ..."],
  "your_answer": "what student picked — use correct_answer_hint/your_answer_hint if provided, otherwise extract from raw_text. If a platform shows the answer as a number (e.g. '3'), resolve it to the full option text.",
  "correct_answer": "correct answer — same rule as your_answer. Always give the full option text, not just a number.",
  "solution_text": "CRITICAL: Copy the platform's original solution VERBATIM from extracted_solution. Preserve ALL step-by-step calculations, intermediate steps (e.g. '14589 - 13511 = 1078'), formulas, and the final answer line. DO NOT rewrite, summarize, shorten, or paraphrase it. Only clean up obvious formatting garbage (stray HTML entities, duplicate whitespace). If the original has 'Given:', 'Formula Used:', 'Calculation:' sections, keep those section headers. The student needs to see the exact working steps to learn the method.",
  "subject": "EXACTLY one of: Quantitative Aptitude | Reasoning | English | Economics | History | Geography | Polity | General Awareness | Computer Science | Biology | Chemistry | Physics | Other",
  "topic": "main topic like 'Profit and Loss', 'Coding-Decoding', 'Characteristics of Money'",
  "subtopic": "more specific like 'Successive discounts', 'Letter-number coding'",
  "concept_tags": ["2-4 concept tags for revision"],
  "mistake_type": "EXACTLY one of: concept_gap | silly_mistake | tricky_option | formula_confusion | time_pressure | unknown",
  "difficulty": "EXACTLY one of: easy | medium | hard",
  "revision_note": "1-2 sentence core rule/concept to remember next time. This is ADDITIONAL to the solution, a brief takeaway. Direct English."
}

FRACTIONS & MATH FORMATTING:
- Scraped option/answer text sometimes has fractions flattened into one run of digits
  (e.g. a mixed fraction "12 74/255" arrives as "1274255"). When the correct_answer or
  solution makes the intended fraction unambiguous, restore the readable form
  ("12 74/255" or "12 3/4"), keeping it consistent across all options.
- Write fractions as "a/b" or mixed "w a/b" (you may use LaTeX \\frac{a}{b}); never
  concatenate numerator and denominator into a single number.
- Keep units (litres, kg, ₹, %) attached to the value, e.g. "12 74/255 litres".

IMAGES:
- Tokens like "[Image: filename.png]" mark figures from the original question (the app
  renders the real image next to the text). Copy these tokens VERBATIM into
  question_text / options / solution_text at the same position. NEVER delete them,
  never merge them, and never invent a description of what the image shows.
- An option that is only "[Image: ...]" is a valid image option — keep it as-is.

If a field is unclear, use empty string "" or [] - DO NOT invent.

Raw scraped data:
"""


def _has_scrambled_math(text: str) -> bool:
    """Return True if text looks like KaTeX-scrambled math (chars on separate lines).

    KaTeX renders each character as an absolutely-positioned span. innerText reads
    them in DOM order, so an expression like √(9+4√5) ends up as individual lines:
    '9+4', '5', '—', '√', ')', '—', '—', ...

    Strong signals: an isolated '√' line, or 2+ isolated '—' / '–' bar lines.
    Weak signal: ≥30% of lines are a single math character (digit, operator, etc).
    """
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if len(lines) < 4:
        return False
    has_sqrt_line  = any(l == '√' for l in lines)
    bar_line_count = sum(1 for l in lines if l in ('—', '–', '─', '_'))
    single_math    = sum(1 for l in lines if len(l) == 1 and l in '0123456789√+−×÷=()—–abcxyn')
    high_frag      = len(lines) >= 6 and single_math / len(lines) > 0.30
    return has_sqrt_line or bar_line_count >= 2 or high_frag


def _heuristic_fallback(raw_question: dict) -> dict:
    """Used when Ollama is unreachable. Best-effort extraction from scraped fields."""
    raw_text = raw_question.get("raw_text", "")
    import re
    your_m = re.search(r"(?:your\s*answer|you\s*(?:chose|selected)|attempted)\s*:?\s*([^\n]{1,120})", raw_text, re.I)
    correct_m = re.search(r"(?:correct\s*answer|right\s*answer)\s*:?\s*([^\n]{1,120})", raw_text, re.I)

    return {
        "question_text": (raw_question.get("question_text") or raw_text)[:1000].strip(),
        "options": raw_question.get("options", []),
        # Prefer the pre-parsed hints from the content script (already cleaned up)
        # over re-running regex on raw_text which captures trailing garbage like "is ₹24582."
        "your_answer": raw_question.get("your_answer_hint") or (your_m.group(1).strip() if your_m else ""),
        "correct_answer": raw_question.get("correct_answer_hint") or (correct_m.group(1).strip() if correct_m else ""),
        "solution_text": raw_question.get("solution_text", "")[:2000],
        "subject": "Other",
        "topic": "Uncategorized",
        "subtopic": "",
        "concept_tags": [],
        "mistake_type": "unknown",
        "difficulty": "medium",
        "revision_note": "Ollama unavailable — question saved without auto-categorization. Start Ollama and re-ingest for full analysis.",
    }


def _extract_json(text: str) -> Optional[dict]:
    """Try multiple strategies to pull JSON from a model response."""
    text = text.strip()
    if not text:
        return None
    # Strip markdown fences
    if text.startswith("```"):
        lines = text.split("\n")
        # remove first and last fence lines
        text = "\n".join(lines[1:-1]) if lines[-1].startswith("```") else "\n".join(lines[1:])
        text = text.strip()
    # Direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Find the first {...} block (greedy match to last })
    import re
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return None


def structure_with_ollama(raw_question: dict) -> Optional[dict]:
    """Takes a raw scraped question dict, returns structured data via Ollama (local or cloud)."""
    raw_payload = {
        "raw_text": raw_question.get("raw_text", "")[:8000],
        "extracted_question": raw_question.get("question_text", ""),
        "extracted_options": raw_question.get("options", []),
        "extracted_solution": raw_question.get("solution_text", ""),
        "correct_answer_hint": raw_question.get("correct_answer_hint", ""),
        "your_answer_hint": raw_question.get("your_answer_hint", ""),
        "status": raw_question.get("status", "wrong"),
    }

    try:
        with httpx.Client(timeout=OLLAMA_TIMEOUT) as c:
            r = c.post(
                f"{OLLAMA_URL}/api/generate",
                json={
                    "model": OLLAMA_MODEL,
                    "prompt": STRUCTURE_PROMPT + json.dumps(raw_payload, ensure_ascii=False),
                    # "json" mode = return any valid JSON. Works on both local and cloud.
                    # Previously passed a JSON schema object but cloud endpoint rejects that.
                    "format": "json",
                    "stream": False,
                    "options": {
                        "temperature": 0.2,
                        "num_predict": 4000,  # allow longer verbatim solutions
                    },
                },
            )
        r.raise_for_status()
        response_json = r.json()
        response_text = (response_json.get("response") or "").strip()

        if not response_text:
            print(f"⚠️  Ollama returned empty response. Full response: {response_json}")
            return _heuristic_fallback(raw_question)

        data = _extract_json(response_text)
        if not data:
            print(f"⚠️  Could not parse JSON from Ollama response.")
            print(f"   First 500 chars: {response_text[:500]}")
            return _heuristic_fallback(raw_question)

        if not data.get("question_text"):
            # Claude might have returned partial data; still useful but merge with fallback
            data["question_text"] = raw_question.get("question_text") or raw_question.get("raw_text", "")[:500]
        return data

    except httpx.HTTPStatusError as e:
        if e.response.status_code in (401, 403):
            print(f"⚠️  Ollama Cloud auth error ({e.response.status_code}). Run: ollama signin")
        elif e.response.status_code == 429:
            print(f"⚠️  Ollama Cloud rate limit hit. Switch to local model:")
            print(f"   set OLLAMA_MODEL=qwen2.5:7b-instruct && ollama pull qwen2.5:7b-instruct")
        else:
            print(f"⚠️  Ollama HTTP {e.response.status_code}: {e.response.text[:200]}")
        return _heuristic_fallback(raw_question)
    except Exception as e:
        print(f"⚠️  Ollama structuring failed ({type(e).__name__}): {e}")
        return _heuristic_fallback(raw_question)


# ---------- Spaced Repetition ----------
def update_sr(error_row: dict, quality: int) -> dict:
    """
    Custom SRS:
      quality 0 = Again  → reset, back tomorrow
      quality 1 = Hard   → short bump, ease penalty
      quality 2 = Good   → standard graduated intervals
      quality 3 = Easy   → starts at 7 days, grows faster (×1.3 bonus on top of ease)

    Key design choices vs plain SM-2:
    - Easy: 4d → 10d → grows with ease×1.3 (no 1-day start, no 21-day jump)
    - Good: 3d → 7d → grows with ease (standard SM-2 steps)
    - Hard: interval×1.2 each time, ease decreases — never resets reps
    - Again: full reset to 1 day, ease penalty
    - Max interval: 45 days (exam prep — nothing disappears for >6 weeks)
    - ±15% jitter prevents 500 same-day cards from all piling up on the same due date
    - Mastered = 4+ reviews with interval ≥ 21 days on a Good/Easy rating
    """
    import random

    ease = error_row["sr_ease"] or 2.5
    reps  = error_row["sr_reps"] or 0
    interval = error_row["sr_interval"] or 0
    MAX_INTERVAL = 45  # exam prep: nothing disappears for more than 6 weeks

    if quality == 0:  # Again — full reset, back tomorrow
        reps = 0
        interval = 1
        ease = max(1.3, ease - 0.2)

    elif quality == 1:  # Hard — small bump, ease penalty, never resets reps
        reps += 1
        interval = max(1, round(interval * 1.2)) if interval > 0 else 2
        ease = max(1.3, ease - 0.15)

    elif quality == 2:  # Good — standard graduated steps
        reps += 1
        if reps == 1:
            interval = 3
        elif reps == 2:
            interval = 7
        else:
            interval = round(interval * ease)

    else:  # Easy — confirmed easy, grow faster but don't jump too far
        reps += 1
        if reps == 1:
            interval = 4          # 4 days: confirm it's truly easy before spacing far
        elif reps == 2:
            interval = 10         # 10 days: second confirmation
        else:
            interval = round(interval * ease * 1.3)   # grows ~30% faster than Good
        ease = min(3.5, ease + 0.15)

    interval = min(interval, MAX_INTERVAL)

    # ±15% jitter on intervals ≥ 3 days to spread due dates across a range,
    # preventing 500 cards logged on the same day from all piling up together.
    if interval >= 3:
        jitter = round(interval * random.uniform(-0.15, 0.15))
        interval = max(1, interval + jitter)

    due = ist_midnight_utc(interval)
    # Mastered when interval has grown past 3 weeks (on at least 4 reviews)
    mastered = 1 if reps >= 4 and interval >= 21 and quality >= 2 else 0

    return {
        "sr_interval": interval,
        "sr_ease": round(ease, 2),
        "sr_reps": reps,
        "sr_due_at": due.isoformat(),
        "sr_last_reviewed": datetime.utcnow().isoformat(),
        "mastered": mastered,
    }


# ---------- FastAPI ----------
app = FastAPI(title="Error Logger")

# Ollama can only meaningfully process one request at a time on a single GPU.
# Without this, logging 20 questions at once spawns 20 concurrent HTTP connections
# which makes Ollama frantically start/kill runner processes (visible in server.log).
_ollama_sem = asyncio.Semaphore(1)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # open — nginx handles access control at the network edge
    allow_methods=["*"],
    allow_headers=["*"],
)


# Chrome 117+ Private Network Access (PNA) policy requires this header for
# HTTPS pages (like testbook.com) to call HTTP localhost. Without it, Chrome
# blocks with "Permission was denied for this request to access the `loopback`
# address space." See chromestatus.com/feature/5436853517811712
@app.middleware("http")
async def add_pna_header(request: Request, call_next):
    # Handle preflight OPTIONS request directly
    if request.method == "OPTIONS":
        from fastapi.responses import Response
        return Response(
            status_code=200,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type, Authorization",
                "Access-Control-Allow-Private-Network": "true",
                "Access-Control-Max-Age": "86400",
            },
        )
    response = await call_next(request)
    response.headers["Access-Control-Allow-Private-Network"] = "true"
    return response


class IngestQuestion(BaseModel):
    raw_text: str = ""
    raw_html: str = ""
    question_text: str = ""
    options: list = []
    solution_text: str = ""
    status: str = "wrong"
    correct_answer_hint: str = ""
    your_answer_hint: str = ""
    question_images: list = []
    option_images: list = []  # list of lists, aligned with options
    solution_images: list = []
    source_url: str = ""  # per-question deep link back to the platform's solution page


class IngestPayload(BaseModel):
    meta: dict
    questions: list[IngestQuestion]


async def _enrich_question_bg(error_id: int, raw_question: dict):
    """Run Ollama structuring in background and update the saved DB record.
    Serialised via _ollama_sem so multiple simultaneous log actions don't
    hammer Ollama with concurrent connections (causes runner spawn storms)."""
    try:
        async with _ollama_sem:
            loop = asyncio.get_event_loop()
            structured = await loop.run_in_executor(None, structure_with_ollama, raw_question)
        if not structured or structured.get("topic") in ("Uncategorized", "", None):
            return
        with db() as conn:
            conn.execute(
                """UPDATE errors SET
                    question_text = ?, options_json = ?, your_answer = ?, correct_answer = ?,
                    solution_text = ?, subject = ?, topic = ?, subtopic = ?,
                    concept_tags_json = ?, mistake_type = ?, difficulty = ?
                WHERE id = ?""",
                (
                    structured.get("question_text") or raw_question.get("question_text", ""),
                    json.dumps(structured.get("options", [])),
                    structured.get("your_answer", ""),
                    structured.get("correct_answer", ""),
                    structured.get("solution_text", "") + "\n\n💡 " + structured.get("revision_note", ""),
                    structured.get("subject", "Other"),
                    structured.get("topic", ""),
                    structured.get("subtopic", ""),
                    json.dumps(structured.get("concept_tags", [])),
                    structured.get("mistake_type", "unknown"),
                    structured.get("difficulty", "medium"),
                    error_id,
                ),
            )
        print(f"✅ AI enrichment done for id={error_id}: {structured.get('topic', '?')}")
    except Exception as e:
        print(f"⚠️  Background enrichment failed for id={error_id}: {e}")


@app.post("/api/ingest")
async def ingest(payload: IngestPayload):
    saved = 0
    skipped_dup = 0
    errors_list = []
    bg_tasks = []

    for q in payload.questions:
        if not (q.raw_text or q.question_text):
            continue

        # Save immediately with heuristic data — no waiting for Ollama
        heuristic = _heuristic_fallback(q.model_dump())
        qtext = heuristic.get("question_text", "").strip()
        if not qtext:
            continue

        # Hash from raw input so it's stable regardless of AI restructuring
        hash_input = (q.raw_text or q.question_text).strip()
        qhash = hashlib.sha256(hash_input.encode()).hexdigest()[:32]

        with db() as conn:
            existing = conn.execute("SELECT id FROM errors WHERE hash = ?", (qhash,)).fetchone()
        if existing:
            skipped_dup += 1
            continue

        # Download figures to the local images folder (offline-safe flashcards).
        # Runs in a thread so the event loop isn't blocked by network I/O.
        loop = asyncio.get_event_loop()
        q_imgs = await loop.run_in_executor(None, _localize_images, q.question_images)
        opt_imgs = await loop.run_in_executor(None, _localize_option_images, q.option_images)
        sol_imgs = await loop.run_in_executor(None, _localize_images, q.solution_images)

        with db() as conn:
            conn.execute(
                """INSERT INTO errors (
                    hash, question_text, options_json, your_answer, correct_answer,
                    solution_text, subject, topic, subtopic, concept_tags_json,
                    mistake_type, difficulty, test_title, test_url, captured_at,
                    session_type, platform, sr_due_at,
                    question_images_json, option_images_json, solution_images_json,
                    source_url
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    qhash,
                    qtext,
                    json.dumps(heuristic.get("options", [])),
                    heuristic.get("your_answer", ""),
                    heuristic.get("correct_answer", ""),
                    heuristic.get("solution_text", ""),
                    "Other",
                    "Uncategorized",
                    "",
                    json.dumps([]),
                    "unknown",
                    "medium",
                    payload.meta.get("test_title", ""),
                    payload.meta.get("test_url", ""),
                    payload.meta.get("captured_at", datetime.utcnow().isoformat()),
                    payload.meta.get("session_type", "mock"),
                    payload.meta.get("platform", "testbook"),
                    datetime.utcnow().isoformat(),
                    json.dumps(q_imgs),
                    json.dumps(opt_imgs),
                    json.dumps(sol_imgs),
                    q.source_url or "",
                ),
            )
            new_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            saved += 1
            errors_list.append({"topic": "pending", "mistake_type": "unknown"})
            if OLLAMA_READY:
                bg_tasks.append((new_id, q.model_dump()))

    # Fire-and-forget: enrich with Ollama in background, user gets instant response
    for error_id, raw_q in bg_tasks:
        asyncio.create_task(_enrich_question_bg(error_id, raw_q))

    return {"saved": saved, "skipped_duplicates": skipped_dup, "errors": errors_list}


@app.get("/api/stats")
async def stats():
    with db() as conn:
        total = conn.execute("SELECT COUNT(*) c FROM errors").fetchone()["c"]
        pending = conn.execute(
            "SELECT COUNT(*) c FROM errors WHERE mastered = 0 AND (sr_due_at IS NULL OR sr_due_at <= ?)",
            (datetime.utcnow().isoformat(),),
        ).fetchone()["c"]
        mastered = conn.execute("SELECT COUNT(*) c FROM errors WHERE mastered = 1").fetchone()["c"]

        by_subject = [
            dict(r)
            for r in conn.execute(
                "SELECT subject, COUNT(*) count FROM errors GROUP BY subject ORDER BY count DESC"
            ).fetchall()
        ]
        by_topic = [
            dict(r)
            for r in conn.execute(
                "SELECT topic, COUNT(*) count FROM errors WHERE topic != '' GROUP BY topic ORDER BY count DESC LIMIT 10"
            ).fetchall()
        ]
        by_mistake = [
            dict(r)
            for r in conn.execute(
                "SELECT mistake_type, COUNT(*) count FROM errors GROUP BY mistake_type ORDER BY count DESC"
            ).fetchall()
        ]

    return {
        "total": total,
        "pending_review": pending,
        "mastered": mastered,
        "by_subject": by_subject,
        "top_topics": by_topic,
        "by_mistake_type": by_mistake,
    }


@app.get("/api/errors")
async def list_errors(subject: Optional[str] = None, topic: Optional[str] = None, limit: int = 100):
    query = "SELECT * FROM errors WHERE 1=1"
    params = []
    if subject:
        query += " AND subject = ?"
        params.append(subject)
    if topic:
        query += " AND topic = ?"
        params.append(topic)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    with db() as conn:
        rows = [dict(r) for r in conn.execute(query, params).fetchall()]
    for r in rows:
        r["options"] = json.loads(r.pop("options_json") or "[]")
        r["concept_tags"] = json.loads(r.pop("concept_tags_json") or "[]")
        _parse_image_cols(r)
    return {"errors": rows}


@app.get("/api/errors/{error_id}")
async def get_error(error_id: int):
    with db() as conn:
        row = conn.execute("SELECT * FROM errors WHERE id = ?", (error_id,)).fetchone()
    if not row:
        raise HTTPException(404)
    d = dict(row)
    d["options"] = json.loads(d.pop("options_json") or "[]")
    d["concept_tags"] = json.loads(d.pop("concept_tags_json") or "[]")
    d["notes"] = json.loads(d.pop("notes_json") or "{}")
    _parse_image_cols(d)
    return d


@app.get("/api/review/next")
async def next_review(
    subject: Optional[str] = None,
    topic: Optional[str] = None,
    session_type: Optional[str] = None,
    platform: Optional[str] = None,
    flagged_only: bool = False,
):
    """Returns the next card due for review.

    Among all cards that are due (and match the filters), the pick is
    weighted-random by subject/topic weight — higher-weight (higher SSC PYQ
    yield) cards surface more often, but every subject keeps a non-zero chance
    so nothing is starved. Spaced repetition still decides *what* is due.
    """
    where = "FROM errors WHERE mastered = 0 AND (sr_due_at IS NULL OR sr_due_at <= ?)"
    params: list = [datetime.utcnow().isoformat()]
    if subject:
        where += " AND subject = ?"
        params.append(subject)
    if topic:
        where += " AND topic = ?"
        params.append(topic)
    if session_type:
        where += " AND session_type = ?"
        params.append(session_type)
    if platform:
        where += " AND platform = ?"
        params.append(platform)
    if flagged_only:
        where += " AND flagged = 1"

    with db() as conn:
        cands = conn.execute("SELECT id, subject, topic, sr_due_at " + where, params).fetchall()
        if not cands:
            return {"card": None}
        sw, tw = _load_weight_maps()
        # Efraimidis–Spirakis weighted reservoir: key = u ** (1/weight); the row
        # with the largest key wins, and P(win) is proportional to its weight.
        best_id, best_key = cands[0]["id"], -1.0
        for r in cands:
            w = _resolve_weight(r["subject"], r["topic"], sw, tw)
            u = random.random() or 1e-12
            key = u ** (1.0 / w)
            if key > best_key:
                best_key, best_id = key, r["id"]
        row = conn.execute("SELECT * FROM errors WHERE id = ?", (best_id,)).fetchone()
    if not row:
        return {"card": None}
    d = dict(row)
    d["options"] = json.loads(d.pop("options_json") or "[]")
    d["concept_tags"] = json.loads(d.pop("concept_tags_json") or "[]")
    d["notes"] = json.loads(d.pop("notes_json") or "{}")
    _parse_image_cols(d)
    d["weight"] = round(_resolve_weight(d.get("subject"), d.get("topic"), sw, tw), 2)
    d["due_count"] = len(cands)
    return {"card": d}


class ReviewRating(BaseModel):
    error_id: int
    quality: int  # 0-3


@app.post("/api/review/rate")
async def rate_review(r: ReviewRating):
    with db() as conn:
        row = conn.execute("SELECT * FROM errors WHERE id = ?", (r.error_id,)).fetchone()
        if not row:
            raise HTTPException(404)
        updates = update_sr(dict(row), r.quality)
        updates["sr_total_reviews"] = (row["sr_total_reviews"] or 0) + 1
        if r.quality == 0:
            updates["sr_wrong_reviews"] = (row["sr_wrong_reviews"] or 0) + 1

        set_clause = ", ".join(f"{k} = ?" for k in updates)
        conn.execute(f"UPDATE errors SET {set_clause} WHERE id = ?", (*updates.values(), r.error_id))
        conn.execute(
            "INSERT INTO review_log (error_id, quality) VALUES (?, ?)", (r.error_id, r.quality)
        )
    return {"ok": True, **updates}


# ---------- Subject / topic weight config ----------
@app.get("/api/weights")
async def get_weights():
    """Current subject + topic weights, plus live card counts per subject."""
    with db() as conn:
        subs = [dict(r) for r in conn.execute(
            "SELECT subject, weight, is_ga, pyq_share FROM subject_weights "
            "ORDER BY is_ga DESC, weight DESC, subject"
        )]
        tops = [dict(r) for r in conn.execute(
            "SELECT subject, topic, weight FROM topic_weights ORDER BY subject, weight DESC, topic"
        )]
        counts = {r["subject"]: r["c"] for r in conn.execute(
            "SELECT subject, COUNT(*) c FROM errors GROUP BY subject"
        )}
    for s in subs:
        s["card_count"] = counts.get(s["subject"], 0)
    return {"subjects": subs, "topics": tops}


class WeightUpdate(BaseModel):
    kind: str  # 'subject' | 'topic'
    subject: str
    topic: Optional[str] = None
    weight: float


@app.post("/api/weights")
async def set_weight(u: WeightUpdate):
    w = max(float(u.weight), 0.0)
    with db() as conn:
        if u.kind == "topic" and u.topic:
            conn.execute(
                "INSERT INTO topic_weights (subject, topic, weight) VALUES (?,?,?) "
                "ON CONFLICT(subject, topic) DO UPDATE SET weight = excluded.weight",
                (u.subject, u.topic, w),
            )
        else:
            conn.execute(
                "INSERT INTO subject_weights (subject, weight) VALUES (?,?) "
                "ON CONFLICT(subject) DO UPDATE SET weight = excluded.weight",
                (u.subject, w),
            )
    return {"ok": True, "weight": w}


@app.delete("/api/weights/topic")
async def delete_topic_weight(subject: str, topic: str):
    """Remove a topic override so the card falls back to its subject weight."""
    with db() as conn:
        conn.execute("DELETE FROM topic_weights WHERE subject = ? AND topic = ?", (subject, topic))
    return {"ok": True}


@app.get("/api/deck/stats")
async def deck_stats(session_type: Optional[str] = None, platform: Optional[str] = None):
    """Total / due-now / mastered counts for a deck (used by the Revision tab header)."""
    where = "WHERE 1=1"
    p: list = []
    if session_type:
        where += " AND session_type = ?"
        p.append(session_type)
    if platform:
        where += " AND platform = ?"
        p.append(platform)
    now = datetime.utcnow().isoformat()
    with db() as conn:
        total = conn.execute(f"SELECT COUNT(*) c FROM errors {where}", p).fetchone()["c"]
        mastered = conn.execute(f"SELECT COUNT(*) c FROM errors {where} AND mastered = 1", p).fetchone()["c"]
        due = conn.execute(
            f"SELECT COUNT(*) c FROM errors {where} AND mastered = 0 AND (sr_due_at IS NULL OR sr_due_at <= ?)",
            p + [now],
        ).fetchone()["c"]
    return {"total": total, "due": due, "mastered": mastered}


@app.post("/api/reingest")
async def reingest():
    """Re-run Ollama structuring for questions saved during Ollama downtime (topic='Uncategorized')."""
    global OLLAMA_READY
    OLLAMA_READY = _ensure_ollama()
    if not OLLAMA_READY:
        raise HTTPException(status_code=503, detail="Ollama is not reachable and could not be auto-started.")

    with db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM errors WHERE topic = 'Uncategorized' OR (topic = '' AND mistake_type = 'unknown')"
        ).fetchall()]

    if not rows:
        return {"updated": 0, "failed": 0, "total": 0, "message": "No uncategorized questions found."}

    updated = 0
    failed = 0

    for row in rows:
        options = json.loads(row.get("options_json") or "[]")

        # Strip the appended revision note that was added by _heuristic_fallback
        solution_text = row.get("solution_text", "")
        if "\n\n\U0001f4a1 " in solution_text:
            solution_text = solution_text.rsplit("\n\n\U0001f4a1 ", 1)[0]

        raw_question = {
            "raw_text": (
                f"Question: {row['question_text']}\n"
                f"Options: {options}\n"
                f"Your Answer: {row.get('your_answer', '')}\n"
                f"Correct Answer: {row.get('correct_answer', '')}\n"
                f"Solution: {solution_text}"
            ),
            "question_text": row["question_text"],
            "options": options,
            "solution_text": solution_text,
            "correct_answer_hint": row.get("correct_answer", ""),
            "your_answer_hint": row.get("your_answer", ""),
            "status": "wrong",
        }

        structured = structure_with_ollama(raw_question)
        if not structured or structured.get("topic") in ("Uncategorized", "", None):
            failed += 1
            continue

        with db() as conn:
            conn.execute(
                """UPDATE errors SET
                    options_json = ?, your_answer = ?, correct_answer = ?,
                    solution_text = ?, subject = ?, topic = ?, subtopic = ?,
                    concept_tags_json = ?, mistake_type = ?, difficulty = ?
                WHERE id = ?""",
                (
                    json.dumps(structured.get("options", options)),
                    structured.get("your_answer", row.get("your_answer", "")),
                    structured.get("correct_answer", row.get("correct_answer", "")),
                    structured.get("solution_text", "") + "\n\n\U0001f4a1 " + structured.get("revision_note", ""),
                    structured.get("subject", "Other"),
                    structured.get("topic", ""),
                    structured.get("subtopic", ""),
                    json.dumps(structured.get("concept_tags", [])),
                    structured.get("mistake_type", "unknown"),
                    structured.get("difficulty", "medium"),
                    row["id"],
                ),
            )
            updated += 1

    with db() as conn:
        total_count = conn.execute("SELECT COUNT(*) FROM errors").fetchone()[0]
    skipped = total_count - len(rows)
    return {"updated": updated, "failed": failed, "needs_reclean": len(rows),
            "skipped": skipped, "total": total_count}


@app.get("/api/reclean/count")
async def reclean_count():
    """Return how many questions have scrambled math text (need re-cleaning)."""
    with db() as conn:
        rows = conn.execute("SELECT id, question_text FROM errors").fetchall()
    n = sum(1 for r in rows if _has_scrambled_math(r["question_text"] or ""))
    return {"needs_reclean": n, "total": len(rows)}


@app.post("/api/reclean")
async def reclean():
    """Re-run Ollama only on questions whose text shows KaTeX-scrambled math.
    Skips questions that look fine — much faster than re-processing everything."""
    global OLLAMA_READY
    OLLAMA_READY = _ensure_ollama()
    if not OLLAMA_READY:
        raise HTTPException(status_code=503, detail="Ollama is not reachable.")

    with db() as conn:
        all_rows = [dict(r) for r in conn.execute("SELECT * FROM errors").fetchall()]

    rows = [r for r in all_rows if _has_scrambled_math(r.get("question_text") or "")]

    if not rows:
        return {"updated": 0, "skipped": len(all_rows), "failed": 0, "total": len(all_rows),
                "message": "No questions with scrambled math found — nothing to do."}

    updated = 0
    failed = 0
    for row in rows:
        options = json.loads(row.get("options_json") or "[]")
        solution_text = row.get("solution_text", "")
        if "\n\n\U0001f4a1 " in solution_text:
            solution_text = solution_text.rsplit("\n\n\U0001f4a1 ", 1)[0]

        raw_question = {
            "raw_text": (
                f"Question: {row['question_text']}\n"
                f"Options: {options}\n"
                f"Your Answer: {row.get('your_answer', '')}\n"
                f"Correct Answer: {row.get('correct_answer', '')}\n"
                f"Solution: {solution_text}"
            ),
            "question_text": row["question_text"],
            "options": options,
            "solution_text": solution_text,
            "correct_answer_hint": row.get("correct_answer", ""),
            "your_answer_hint": row.get("your_answer", ""),
            "status": "wrong",
        }

        structured = structure_with_ollama(raw_question)
        if not structured or not structured.get("question_text"):
            failed += 1
            continue

        with db() as conn:
            conn.execute(
                """UPDATE errors SET
                    question_text = ?, options_json = ?, your_answer = ?, correct_answer = ?,
                    solution_text = ?, subject = ?, topic = ?, subtopic = ?,
                    concept_tags_json = ?, mistake_type = ?, difficulty = ?
                WHERE id = ?""",
                (
                    structured.get("question_text", row["question_text"]),
                    json.dumps(structured.get("options", options)),
                    structured.get("your_answer", row.get("your_answer", "")),
                    structured.get("correct_answer", row.get("correct_answer", "")),
                    structured.get("solution_text", "") + "\n\n\U0001f4a1 " + structured.get("revision_note", ""),
                    structured.get("subject", row.get("subject", "Other")),
                    structured.get("topic", row.get("topic", "")),
                    structured.get("subtopic", row.get("subtopic", "")),
                    json.dumps(structured.get("concept_tags", [])),
                    structured.get("mistake_type", row.get("mistake_type", "unknown")),
                    structured.get("difficulty", row.get("difficulty", "medium")),
                    row["id"],
                ),
            )
            updated += 1

    return {"updated": updated, "failed": failed, "total": len(rows)}


@app.post("/api/errors/{error_id}/reclean")
async def reclean_one(error_id: int):
    """Re-run Ollama on a single question to fix scrambled math text.
    Synchronous from the caller's perspective (~30s). Returns the updated record."""
    global OLLAMA_READY
    OLLAMA_READY = _ensure_ollama()
    if not OLLAMA_READY:
        raise HTTPException(status_code=503, detail="Ollama is not reachable.")

    with db() as conn:
        row = conn.execute("SELECT * FROM errors WHERE id = ?", (error_id,)).fetchone()
    if not row:
        raise HTTPException(404)
    row = dict(row)

    options = json.loads(row.get("options_json") or "[]")
    solution_text = row.get("solution_text", "")
    if "\n\n\U0001f4a1 " in solution_text:
        solution_text = solution_text.rsplit("\n\n\U0001f4a1 ", 1)[0]

    raw_question = {
        "raw_text": (
            f"Question: {row['question_text']}\n"
            f"Options: {options}\n"
            f"Your Answer: {row.get('your_answer', '')}\n"
            f"Correct Answer: {row.get('correct_answer', '')}\n"
            f"Solution: {solution_text}"
        ),
        "question_text": row["question_text"],
        "options": options,
        "solution_text": solution_text,
        "correct_answer_hint": row.get("correct_answer", ""),
        "your_answer_hint": row.get("your_answer", ""),
        "status": "wrong",
    }

    loop = asyncio.get_event_loop()
    async with _ollama_sem:
        structured = await loop.run_in_executor(None, structure_with_ollama, raw_question)

    if not structured or not structured.get("question_text"):
        raise HTTPException(status_code=500, detail="Ollama returned no usable data.")

    with db() as conn:
        conn.execute(
            """UPDATE errors SET
                question_text = ?, options_json = ?, your_answer = ?, correct_answer = ?,
                solution_text = ?, subject = ?, topic = ?, subtopic = ?,
                concept_tags_json = ?, mistake_type = ?, difficulty = ?
            WHERE id = ?""",
            (
                structured.get("question_text", row["question_text"]),
                json.dumps(structured.get("options", options)),
                structured.get("your_answer", row.get("your_answer", "")),
                structured.get("correct_answer", row.get("correct_answer", "")),
                structured.get("solution_text", "") + "\n\n\U0001f4a1 " + structured.get("revision_note", ""),
                structured.get("subject", row.get("subject", "Other")),
                structured.get("topic", row.get("topic", "")),
                structured.get("subtopic", row.get("subtopic", "")),
                json.dumps(structured.get("concept_tags", [])),
                structured.get("mistake_type", row.get("mistake_type", "unknown")),
                structured.get("difficulty", row.get("difficulty", "medium")),
                error_id,
            ),
        )
        updated = conn.execute("SELECT * FROM errors WHERE id = ?", (error_id,)).fetchone()

    d = dict(updated)
    d["options"] = json.loads(d.pop("options_json") or "[]")
    d["concept_tags"] = json.loads(d.pop("concept_tags_json") or "[]")
    d["notes"] = json.loads(d.pop("notes_json") or "{}")
    _parse_image_cols(d)
    return d


@app.delete("/api/errors/{error_id}")
async def delete_error(error_id: int):
    with db() as conn:
        conn.execute("DELETE FROM errors WHERE id = ?", (error_id,))
    return {"ok": True}


@app.patch("/api/errors/{error_id}/flag")
async def toggle_flag(error_id: int):
    with db() as conn:
        row = conn.execute("SELECT flagged FROM errors WHERE id = ?", (error_id,)).fetchone()
        if not row:
            raise HTTPException(404)
        new_val = 0 if row["flagged"] else 1
        conn.execute("UPDATE errors SET flagged = ? WHERE id = ?", (new_val, error_id))
    return {"ok": True, "flagged": new_val}


class ClassifyPayload(BaseModel):
    subject: Optional[str] = None
    topic: Optional[str] = None
    subtopic: Optional[str] = None
    mistake_type: Optional[str] = None
    difficulty: Optional[str] = None


@app.patch("/api/errors/{error_id}/classify")
async def classify_error(error_id: int, payload: ClassifyPayload):
    fields = {k: v for k, v in payload.model_dump().items() if v is not None}
    if not fields:
        return {"ok": True}
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    with db() as conn:
        if not conn.execute("SELECT id FROM errors WHERE id = ?", (error_id,)).fetchone():
            raise HTTPException(404)
        conn.execute(f"UPDATE errors SET {set_clause} WHERE id = ?", (*fields.values(), error_id))
    return {"ok": True}


class NotesPayload(BaseModel):
    my_mistake: str = ""
    remember_this: str = ""
    formula: str = ""


@app.patch("/api/errors/{error_id}/notes")
async def update_notes(error_id: int, notes: NotesPayload):
    with db() as conn:
        conn.execute(
            "UPDATE errors SET notes_json = ? WHERE id = ?",
            (json.dumps(notes.model_dump()), error_id),
        )
    return {"ok": True}


# ---------- Browse endpoints ----------

@app.get("/api/browse/subjects")
async def browse_subjects():
    with db() as conn:
        rows = conn.execute("""
            SELECT subject,
                   COUNT(*) total,
                   SUM(CASE WHEN session_type='dpps' THEN 1 ELSE 0 END) dpps,
                   SUM(CASE WHEN session_type='mock' THEN 1 ELSE 0 END) mock,
                   SUM(mastered) mastered
            FROM errors
            WHERE subject IS NOT NULL AND subject != ''
            GROUP BY subject ORDER BY total DESC
        """).fetchall()
    return {"subjects": [dict(r) for r in rows]}


@app.get("/api/browse/topics")
async def browse_topics(subject: str):
    with db() as conn:
        rows = conn.execute("""
            SELECT topic,
                   COUNT(*) total,
                   SUM(CASE WHEN session_type='dpps' THEN 1 ELSE 0 END) dpps,
                   SUM(CASE WHEN session_type='mock' THEN 1 ELSE 0 END) mock,
                   SUM(mastered) mastered
            FROM errors
            WHERE subject = ? AND topic IS NOT NULL AND topic != ''
            GROUP BY topic ORDER BY total DESC
        """, (subject,)).fetchall()
    return {"topics": [dict(r) for r in rows]}


@app.get("/api/browse/subtopics")
async def browse_subtopics(subject: str, topic: str):
    with db() as conn:
        rows = conn.execute("""
            SELECT subtopic,
                   COUNT(*) total,
                   SUM(CASE WHEN session_type='dpps' THEN 1 ELSE 0 END) dpps,
                   SUM(CASE WHEN session_type='mock' THEN 1 ELSE 0 END) mock,
                   SUM(mastered) mastered
            FROM errors
            WHERE subject = ? AND topic = ? AND subtopic IS NOT NULL AND subtopic != ''
            GROUP BY subtopic ORDER BY total DESC
        """, (subject, topic)).fetchall()
    return {"subtopics": [dict(r) for r in rows]}


@app.get("/api/browse/cards")
async def browse_cards(
    subject: Optional[str] = None,
    topic: Optional[str] = None,
    subtopic: Optional[str] = None,
    session_type: Optional[str] = None,
    flagged_only: bool = False,
    limit: int = 100,
):
    query = "SELECT * FROM errors WHERE 1=1"
    params: list = []
    if subject:
        query += " AND subject = ?"
        params.append(subject)
    if topic:
        query += " AND topic = ?"
        params.append(topic)
    if subtopic:
        query += " AND subtopic = ?"
        params.append(subtopic)
    if session_type:
        query += " AND session_type = ?"
        params.append(session_type)
    if flagged_only:
        query += " AND flagged = 1"
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    with db() as conn:
        rows = [dict(r) for r in conn.execute(query, params).fetchall()]
    for r in rows:
        r["options"] = json.loads(r.pop("options_json") or "[]")
        r["concept_tags"] = json.loads(r.pop("concept_tags_json") or "[]")
        r["notes"] = json.loads(r.pop("notes_json") or "{}")
        _parse_image_cols(r)
    return {"cards": rows}


# ---------- Analytics endpoints ----------

@app.get("/api/weakzones")
async def weakzones(limit: int = 10):
    """Subtopics ranked by error count weighted against mastery rate."""
    with db() as conn:
        rows = conn.execute("""
            SELECT subject, topic, subtopic,
                   COUNT(*) total,
                   SUM(mastered) mastered,
                   ROUND(100.0 * SUM(mastered) / COUNT(*), 1) mastery_pct
            FROM errors
            WHERE subtopic IS NOT NULL AND subtopic != ''
            GROUP BY subject, topic, subtopic
            ORDER BY total DESC, mastery_pct ASC
            LIMIT ?
        """, (limit,)).fetchall()
    return {"weakzones": [dict(r) for r in rows]}


@app.get("/api/trends")
async def trends(subject: Optional[str] = None, days: int = 30):
    """Daily counts of errors logged and mastered over the last N days."""
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    base = "FROM errors WHERE created_at >= ?"
    params: list = [cutoff]
    if subject:
        base += " AND subject = ?"
        params.append(subject)
    with db() as conn:
        logged = conn.execute(
            f"SELECT DATE(created_at) d, COUNT(*) n {base} GROUP BY d ORDER BY d",
            params,
        ).fetchall()
        mastered = conn.execute(
            f"SELECT DATE(sr_last_reviewed) d, COUNT(*) n {base} AND mastered=1 AND sr_last_reviewed IS NOT NULL GROUP BY d ORDER BY d",
            params,
        ).fetchall()
    return {
        "logged": [dict(r) for r in logged],
        "mastered": [dict(r) for r in mastered],
    }


# ---------- Static webapp ----------
@app.get("/", response_class=HTMLResponse)
async def index():
    return FileResponse(WEBAPP_DIR / "index.html")


if WEBAPP_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(WEBAPP_DIR)), name="static")

# Locally-downloaded question/option/solution figures (see _localize_image)
IMAGES_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/images", StaticFiles(directory=str(IMAGES_DIR)), name="images")


if __name__ == "__main__":
    print(f"🚀 Error Logger running at http://localhost:{PORT}")
    print(f"   Dashboard: http://localhost:{PORT}/")
    # Loud DB banner: if you ever see a surprising error count, check THIS line first.
    try:
        _c = sqlite3.connect(DB_PATH)
        _n = _c.execute("SELECT COUNT(*) FROM errors").fetchone()[0]
        _c.close()
        print(f"   📂 DB: {DB_PATH}")
        print(f"   📊 Loaded {_n} errors from this database")
    except Exception as _e:
        print(f"   📂 DB: {DB_PATH}  (could not read count: {_e})")
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="info")

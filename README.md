# Error Logger

Auto-capture your mock-test mistakes → structured flashcards → spaced repetition
revision. **Zero typing, zero API costs** (uses Ollama Cloud's free tier).

Works on **Testbook**, **Physics Wallah** (`pw.live`, including DPPs), **Oliveboard**
and **TestRanking**.

## What it does

1. **1-click capture** — On any supported solution page, a floating button scrapes wrong/skipped questions
2. **DeepSeek V3.1 categorizes** — Ollama Cloud runs a 671B param model for free (preview tier). Extracts question, answers, subject, topic, mistake type, and writes a revision note.
3. **SQLite storage** — All local, all yours
4. **Flashcard revision** — SM-2 spaced repetition (same algo as Anki). Cards you struggle with show more often
5. **Dashboard** — See your weak topics and mistake patterns at a glance

## Giving this to someone else (one-click install)

If you just want to hand someone a working copy, the latest prebuilt package is
already in [`release/`](release/) — send them that zip and skip to "On their side"
below.

Build the package once:

```powershell
powershell -ExecutionPolicy Bypass -File build-release.ps1
```

That runs PyInstaller, assembles `release/ErrorLogger-Setup/` (installer +
`error-logger-server.exe` + extension), refuses to build if your `errors.db`, images or
logs sneak in, and zips it. Send them the zip.

On their side: unzip, double-click **Install.bat**. It installs Ollama (winget, else a
silent download), starts it, sets up the model (Ollama Cloud sign-in, or pulls
`qwen2.5:7b-instruct` for offline use), copies the app to
`%LOCALAPPDATA%\ErrorLogger`, makes Desktop/Start-Menu shortcuts and an
optional run-at-login entry, starts the server, then opens `chrome://extensions`
with the extension path already on their clipboard for **Load unpacked** — the one
step Chrome will not let a script do. No admin rights, nothing system-wide.
`Uninstall.bat` reverses it and asks before deleting their flashcards.

Useful flags on `install.ps1`: `-Mode cloud|local` (skip the model prompt),
`-Silent`, `-SkipModel` (repair an install without re-downloading), `-NoStart`,
`-NoBrowser`. Re-running is safe and never touches the database.

The installer also works straight from a copy of this repo — run
`installer/Install.bat`; with no prebuilt exe present it builds a Python venv from
source instead.

## Setup (one-time, ~10 min) — manual / development

### 1. Install Ollama (v0.12 or later)

**Mac/Linux:** `curl -fsSL https://ollama.com/install.sh | sh`
**Windows:** Download from https://ollama.com

Verify version: `ollama --version` → must be 0.12 or higher for Cloud support.

### 2. Sign in to Ollama Cloud

```bash
ollama signin
```

This opens a browser to authenticate your Ollama account. Creates a free account if you don't have one. After signin, cloud models are accessible through your **local** `localhost:11434` endpoint — Ollama proxies cloud calls transparently.

### 3. Verify cloud model access

```bash
ollama run deepseek-v3.1:671b-cloud "hi"
```

Should respond within 10-20 seconds. If you get auth errors, re-run `ollama signin`.

### 4. Make sure Ollama is running

```bash
ollama serve
```

(Usually auto-starts as a background service after install — if so, skip this.)

### 5. Install the server

```bash
cd server
python3 -m venv venv
source venv/bin/activate    # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 6. Run the server

```bash
python app.py
```

Dashboard opens at http://localhost:8787/

### 7. Install the Chrome extension

1. Open Chrome → `chrome://extensions/`
2. Enable **Developer mode** (top right)
3. Click **Load unpacked** → select the `extension/` folder
4. Pin the extension to your toolbar

## Daily workflow

1. Give your mock test as usual
2. On the solution / analysis page, click floating **📝 Log Mistakes** (bottom right) → **Scan page for wrong answers**
3. Wait ~3-8 seconds per wrong question (cloud 671B is fast, but there's network latency)
4. Open http://localhost:8787/ → **Review** tab → flashcards

### Oliveboard

Oliveboard's solution viewer shows **one question at a time** and only decodes a
question's content when you navigate to it (the other questions sit in the DOM as
encoded placeholders). So the workflow is slightly different:

1. Finish a mock, open **Solution & Analysis → View Solutions** to land on the
   question-wise solution app (`.../exams/solution/index3.php?...`).
2. Click **📝 Log Mistakes → Scan page for wrong answers**. The extension reads the
   answer-map palette, then walks through every **Wrong** and **Unattempted** question
   for you (clicking each one so Oliveboard decodes it), scraping question, options,
   your pick, the correct option, and the solution. A `Reading Oliveboard solutions… n/N`
   toast shows progress; you're returned to your original question when it finishes.
3. Or open a single question and use **📌 Log current question only**.

Notes:
- **Wrong** questions capture your incorrect pick (`.opt.wrong`) and the correct
  option (`.opt.correct`) directly. **Unattempted** questions aren't marked with a
  correct option in the DOM, so the correct answer is left for the server model to pull
  from the solution text.
- A minority of questions (some quant/diagram items) render options as **images**;
  their option text comes through blank, but the raw HTML is still sent and the
  question/solution text is captured.

## Configuration

Set these env vars before running `python app.py`:

| Variable | Default | Purpose |
|---|---|---|
| `OLLAMA_URL` | `http://localhost:11434` | Where Ollama is listening |
| `OLLAMA_MODEL` | `deepseek-v3.1:671b-cloud` | Which model to use |
| `OLLAMA_TIMEOUT` | `180` | Max seconds per question |

## Model options

**deepseek-v3.1:671b-cloud** (default) — Free via Ollama Cloud preview. Near-Claude quality for categorization. Requires `ollama signin`. Has rate limits (undocumented, but generous for study use). Thinking mode is disabled in code to save tokens.

**qwen2.5:7b-instruct** (fully local fallback) — If you hit Cloud rate limits or want 100% offline:
```bash
ollama pull qwen2.5:7b-instruct
export OLLAMA_MODEL=qwen2.5:7b-instruct
python app.py
```
Lower quality on subtle categorization but reliable and private.

**gpt-oss:20b-cloud / qwen3-coder:480b-cloud** — Other free Ollama Cloud options. Just swap `OLLAMA_MODEL`.

## Free tier caveats

Ollama Cloud was in **preview** when this was written (2026), free but expected to move to paid eventually — check Ollama's current terms before relying on it. Rate limits were generous (plenty for study use — probably 100+ questions/day easy), but they're not publicly documented and can change.

If you hit a rate limit mid-session, the system shows a clear error pointing you to switch to local mode. No data loss — just stop, switch the env var, restart the server, and resume.

## Troubleshooting

**"Server offline" toast in Chrome**
→ Make sure `python app.py` is running. Consider a systemd service or screen/tmux session to keep it running.

**"Ollama Cloud auth error 401/403"**
→ Run `ollama signin` again. Your cloud auth may have expired.

**"Ollama Cloud rate limit hit" (429)**
→ Either wait an hour or switch to local: `export OLLAMA_MODEL=qwen2.5:7b-instruct`, pull it if needed, and restart. Heuristic fallback also kicks in automatically if cloud is down — questions still save with regex-extracted answers, just without smart categorization.

**"Found 0 questions"**
→ The site's DOM may have changed. Try "Log current question only" — grabs whatever's in viewport.

**Categorization is off**
→ Edit `STRUCTURE_PROMPT` in `server/app.py` to add few-shot examples from your own syllabus. Even DeepSeek benefits from 2-3 examples of the question types you actually sit.

## What happens if Ollama is down?

System degrades gracefully. Questions still get saved with regex-extracted "your answer" and "correct answer" fields. Subject/topic/mistake_type stay as "Uncategorized". You can re-ingest them later once Ollama is back.

## File structure

```
error-logger/
├── extension/               # Chrome extension (Manifest V3)
│   ├── manifest.json
│   ├── background.js        # Service worker
│   ├── content.js           # Scrapes the supported sites' solution pages
│   ├── content.css          # Floating button + toast styles
│   ├── popup.html / popup.js
│   └── icon.png
├── server/                  # FastAPI backend
│   ├── app.py               # Ingest + Ollama + SM-2 + REST API
│   ├── requirements.txt
│   ├── error_logger_server.spec   # PyInstaller build recipe
│   ├── nginx-error-logger.conf    # Optional reverse-proxy config
│   └── error-logger.service       # Optional systemd unit
├── webapp/
│   └── index.html           # Dashboard + flashcard UI (single file, no build step)
├── installer/               # One-click install for other people
│   ├── Install.bat          # what the recipient double-clicks
│   ├── install.ps1          # Ollama + model + server + shortcuts + extension
│   ├── Uninstall.bat / uninstall.ps1
│   ├── setup-guide.html     # Illustrated walkthrough opened after install
│   └── READ-ME-FIRST.txt
├── release/                 # Prebuilt shareable zip (built by build-release.ps1)
└── build-release.ps1        # Builds the shareable zip
```

Not in the repo (created locally, ignored by git): `server/errors.db` — your own
flashcard database — plus `server/venv/`, `server/build/`, `server/dist/` and the
unpacked `release/ErrorLogger-Setup/` staging folder.

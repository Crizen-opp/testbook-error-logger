# Error Logger

**Stop re-making the same mistakes.** After every mock test, one click pulls out every
question you got wrong or skipped, an AI sorts them by subject, topic and *type* of
mistake, and they become flashcards that come back to you exactly when you're about to
forget them.

Everything runs on your own computer. No subscription, no data leaves your PC (except
the question text, if you pick the cloud AI option).

Works on **Testbook**, **Physics Wallah (pw.live)**, **Oliveboard** and **TestRanking**.

---

## Install it

1. **Unzip the whole folder first.** Don't run anything from inside the zip.
2. Double-click **`Install.bat`**.
3. If Windows shows a blue "Windows protected your PC" box:
   click **More info** → **Run anyway**. It says that about every unsigned
   script; nothing here needs admin rights.
4. Answer two questions (pressing Enter picks the recommended option both times):
   - which AI model — see [Which model?](#which-model) below
   - start automatically with Windows?
5. Wait. First install is **5–15 minutes**, almost all of it downloading.
6. At the end Chrome opens on its extensions page. Four clicks:

   | # | Do this |
   |---|---------|
   | 1 | Turn **Developer mode** ON (toggle, top-right) |
   | 2 | Click **Load unpacked** |
   | 3 | Paste the folder path — it's already on your clipboard, just press Ctrl+V — and hit Enter |
   | 4 | Click **Select Folder**, then pin the extension (puzzle icon in the toolbar) |

That's the only manual step. Chrome refuses to let a script install an extension for
you, for good security reasons.

### What the installer actually does

- installs **Ollama** (the thing that runs the AI) if you don't have it
- starts it, and sets up your chosen model
- copies the app to `C:\Users\<you>\AppData\Local\ErrorLogger`
- makes a Desktop and Start Menu shortcut
- starts the server and opens your dashboard
- copies the extension where Chrome can find it

Nothing is installed system-wide. No admin password. Re-running `Install.bat` is safe
any time — it repairs a broken install and never touches your saved flashcards.

---

## Which model?

You're asked this once, during install.

### 1. Ollama Cloud — recommended, the default

Uses **DeepSeek V3.1**, a 671-billion-parameter model, free on Ollama's preview tier.
Nothing to download and clearly the best categorisation.

- one-time browser sign-in during install (free account, made in ~30 seconds)
- needs internet whenever you capture questions
- your question text goes to Ollama's servers for categorising
- generous but undocumented rate limits — fine for normal study, and it tells you
  clearly if you ever hit one

### 2. Fully local

Downloads **qwen2.5:7b-instruct** (~4.7 GB) and runs it on your own machine.

- works offline forever, no account, no limits, nothing ever leaves your PC
- slightly rougher at subtle categorisation
- wants a reasonably modern machine; a GPU makes it much faster

Pick this if you're privacy-conscious or often study without internet. You can switch
later either way — see [Switching the model](#switching-the-model).

---

## Using it every day

1. **Leave the server running.** It's a small black console window. If it's closed,
   open the **Error Logger** shortcut on your Desktop.
2. Give your mock as usual.
3. Open the **solution / analysis** page.
4. Click the floating **📝 Log Mistakes** button (bottom-right) →
   **Scan page for wrong answers**.
5. Wait a few seconds per question while the AI reads them.
6. Open **http://localhost:8787/** → **Review** tab → do your flashcards.

**Capturing just one question:** use **📌 Log current question only** instead.

**On Oliveboard** the extension walks through each wrong and unattempted question by
itself — that site only decodes one question at a time, so it has to click through
them. A progress toast shows `n/N`. Let it finish; it returns you to the question you
started from.

### What the dashboard gives you

- **Review** — flashcards, scheduled by the same spaced-repetition algorithm Anki
  uses. Cards you keep fumbling come back sooner; ones you've mastered fade away.
- **Weak zones** — the topics and mistake types that are actually costing you marks.
- **Browse** — everything you've ever logged, by subject and topic.

---

## Your data

Everything lives in one file:

```
C:\Users\<you>\AppData\Local\ErrorLogger\errors.db
```

Copy it somewhere safe now and then — that single file *is* your entire revision
history. Re-installing or updating never touches it. To move to a new PC, install
there and drop this file in the same place.

---

## Switching the model

Open this file in Notepad:

```
C:\Users\<you>\AppData\Local\ErrorLogger\Start Error Logger.cmd
```

Change the `OLLAMA_MODEL` line to one of:

| Model | What it is |
|---|---|
| `deepseek-v3.1:671b-cloud` | cloud, best quality (default) |
| `gpt-oss:20b-cloud` | cloud, lighter and faster |
| `qwen2.5:7b-instruct` | fully local, offline |

Save, then close and reopen the server window.

Going local for the first time? Run this once in a terminal first:

```
ollama pull qwen2.5:7b-instruct
```

---

## Troubleshooting

**"Server offline" toast in Chrome**
The console window is closed. Open the Desktop shortcut.

**Nothing happens on the solutions page**
The extension isn't loaded or is switched off. Check `chrome://extensions` — it should
be listed and enabled. If it's missing, redo step 6 of the install.

**"auth error 401 / 403"**
Your cloud sign-in expired. Open a terminal (Win+R → `cmd`) and run:

```
ollama signin
```

**"rate limit (429)"**
You've hit the free cloud tier's ceiling. Wait an hour, or switch to the local model
as described above.

**"Found 0 questions"**
The site changed its page layout. Try **Log current question only** — it grabs whatever
is on screen.

**Port 8787 already in use**
Something else took the port. Close it, or ask whoever sent you this to change it.

**Categorisation looks wrong on some questions**
Expected occasionally, especially on local mode and image-heavy quant questions. You
can edit a card from the dashboard.

**Anything else**
Re-run `Install.bat`. It's safe to run any number of times and repairs most problems.

---

## Removing it

Run **`Uninstall.bat`** from this folder. It asks before deleting your flashcards.

Two things it deliberately leaves behind, in case you use them elsewhere:

- **Ollama and its models** — remove from Settings → Apps
- **the Chrome extension** — remove from `chrome://extensions`

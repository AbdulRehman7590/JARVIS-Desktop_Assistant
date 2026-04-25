# JARVIS — Voice-Controlled Desktop Assistant

A modular, voice-controlled desktop assistant for **Windows**, inspired by Iron Man's
J.A.R.V.I.S. It listens, understands intent, asks for permission before doing anything
sensitive, and speaks back. An optional **LLM brain** (Google **Gemini** or any
OpenAI-compatible provider) lets it hold a real conversation **and** turn free-form,
multilingual speech into structured commands.

> Safety first — JARVIS will **never** touch `C:\Windows`, `C:\Program Files`,
> `System32`, or any other system-critical path.

---

## Features

- **Modern PySide6 dashboard** with resizable splitter, floating overlay, mic + send
  buttons, and quick-action chips.
- **Flexible wake word** — _"hey JARVIS"_, plain _"JARVIS"_, plus variants
  (`ok/hi/yo jarvis`) and common mis-transcriptions (`jervis`, `jarvix`).
- **Barge-in** — say _"Jarvis"_ while he's talking to interrupt and immediately ask a
  follow-up. No second wake word needed.
- **14-second follow-up window** after every command (re-arms with each one) so you
  don't keep repeating the wake phrase.
- **Recent-folder memory** — _"open it"_, _"go there"_, _"list it"_ resolve to the
  last folder JARVIS touched (ring buffer of 10 in `data/memory.json`).
- **LLM as primary command interpreter** — speak in any language, free-form phrasing
  works, and trusted regex intents (time/date/joke/volume/show-hide-ui) skip the API
  call so common cases stay snappy and offline.
- **Two LLM backends** — Google Gemini (native `google-genai` SDK, JSON response mode)
  or any OpenAI-compatible HTTP API (OpenAI, Groq, OpenRouter, Together, Ollama,
  LM Studio…). Pick one in **Settings → LLM brain**.
- **Background mode** — _"hey JARVIS hide UI"_ (or `--hidden`) removes the window
  from the taskbar entirely. Recover via tray icon, floating overlay, or _"show UI"_.
- **Three run modes** — GUI, background voice-only listener, or text-only REPL.
- **Voice in / voice out** — SpeechRecognition (Google / Whisper) + Windows SAPI5 /
  pyttsx3.
- **Rule-based intent parser** — fast, offline fallback when the LLM is off or fails.
- **Permission system** — per-category `yes / always / no / never`, persisted to disk.
- **Safety guards** — protected paths, no `shell=True`, mandatory confirmations for
  destructive actions, AST-based safe calculator (no `eval`).
- **File ops** — open, list, search, create, rename, copy, move, delete (confirmed).
- **Whitelisted app launcher**, utilities (time/date/jokes/math/screenshot/sysinfo/
  volume/URLs/web search), scheduler (_"wait 10 minutes then restart"_), rotating
  logs (7-day retention).
- **PyInstaller-ready** — one-file `.exe` build script.

---

## Quick start

### 1. Install Python 3.10 – 3.14 (tested on 3.14.0)

### 2. Install dependencies

Headless / minimal (Python 3.14, voice via `sounddevice`):

```powershell
pip install --upgrade pip
pip install -r requirements-core.txt
```

Full install (adds optional PyAudio + offline Whisper for Python ≤ 3.12):

```powershell
pip install -r requirements.txt
```

> **PyAudio fails on Python 3.13/3.14?** Expected — the `sounddevice` backend takes
> over automatically. **Whisper too heavy?** Comment it out; JARVIS falls back to the
> free Google SpeechRecognition API.

### 3. (Optional) use a virtualenv

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 4. Run

```powershell
python main.py                  # GUI, mic on, wake-word required (default)
python main.py --hidden         # start hidden (recover via tray / "show ui")
python main.py --text-only      # text REPL, no GUI
python main.py --voice-only     # background voice-only listener
python main.py --no-wake        # every utterance is a command (use sparingly)
python main.py --no-mic         # GUI, typing only
python main.py --no-llm         # disable LLM even with API key set
```

> Press `Ctrl+,` for Settings, `Ctrl+H` to hide to background.

---

## Connecting an LLM (optional)

In the GUI: **⚙ Settings → LLM brain**, pick a provider preset, paste your API key,
**Test connection**, **Save**. Saved to `.env` in the project root.

### Google Gemini (recommended)

Get a free key at [Google AI Studio](https://aistudio.google.com/app/apikey):

```env
LLM_PROVIDER=gemini
GEMINI_API_KEY=AIza...your-key...
GEMINI_MODEL=gemini-2.5-flash
```

### OpenAI-compatible providers

```env
LLM_PROVIDER=openai
OPENAI_API_KEY=sk-...your-key...
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_MODEL=gpt-4o-mini
```

When configured, the LLM is the primary command interpreter for anything outside the
fast-path. JARVIS still always **speaks replies in English** for TTS clarity, and
keeps a short rolling memory so follow-ups make sense.

---

## Example commands

(Voice mode with default wake-word; in the typed input the `hey jarvis` prefix is
optional.)

| You say                                                 | What happens                                |
|---------------------------------------------------------|---------------------------------------------|
| "What time is it" / "what is the date"                  | Speaks the answer                           |
| "Tell me a joke"                                        | Built-in joke list                          |
| "What is 12 times 7"                                    | Safe AST calculator                         |
| "System info" / "how is my pc doing"                    | CPU / RAM / OS / disk usage                 |
| "Take a screenshot"                                     | Saved to `data/screenshots/`                |
| "Volume up" / "down" / "mute"                           | Multimedia keys                             |
| "Open google.com" / "search the web for ..."            | Default browser                             |
| "Open my Documents folder" / "list files in Downloads"  | Explorer / reads filenames                  |
| "Search for resume.pdf in D drive"                      | Recursive file search                       |
| "Create a folder named Projects in Documents"           | `FILE_WRITE` permission, then creates       |
| "Create a file notes.txt with this text: buy milk"      | Writes the file                             |
| "Rename / copy / move / delete ..."                     | Confirms, then runs                         |
| "Launch Chrome" / "open notepad"                        | Whitelist + `APP_LAUNCH` perm               |
| "Wait 10 minutes then restart the PC"                   | Async timer + confirmation                  |
| "Wait until notepad.exe closes, then shut down"         | Polls process, confirms                     |
| "Open it" / "go there" / "list it"                      | Most recently touched folder                |
| "Call me Tony" / "my name is Tony"                      | Updates user pill + window title live       |
| **"Hide UI" / "show UI"**                               | Background mode toggle                      |
| "Hello" / "thanks" / "who are you"                      | Friendly chat (no permissions)              |
| "Reset permissions" / "Help" / "Goodbye"                | Self-explanatory                            |

With the LLM on you can also chat freely: _"Who built the Burj Khalifa?"_, etc.

### Wake-word, follow-ups & barge-in

Three natural patterns:

- **One breath:** _"Hey JARVIS, what time is it?"_ → wake stripped, command runs.
- **Two-step:** _"Hey JARVIS"_ → _"Yes?"_ → next phrase becomes the command.
- **Barge-in:** while JARVIS speaks, say _"Jarvis"_ to interrupt; the next phrase is
  taken straight as a follow-up.

After every command, a **14-second follow-up window** lets the next command run
without the wake phrase, and re-arms with each one.

---

## Permission model

| Category         | Examples                                       |
|------------------|------------------------------------------------|
| `FILE_READ`      | list, search, open folder                      |
| `FILE_WRITE`     | create, rename, copy, move, delete, screenshot |
| `APP_LAUNCH`     | launch Chrome / Notepad / CMD / open URL       |
| `SYSTEM_CONTROL` | shutdown, restart, sleep, lock, log off        |

On first use of a category JARVIS asks **yes / always / no / never**. Saved to
`data/permissions.json`. Manage from **⚙ Settings → Permissions** or by saying
_"reset permissions"_.

---

## Safety rules (hard-coded)

- Writes/deletes refused inside `C:\Windows`, `C:\Program Files`,
  `C:\Program Files (x86)`, `C:\ProgramData`, or any `System32` path.
- All file paths normalised & validated before use.
- Apps launch via `subprocess` argv lists — **no `shell=True`**.
- `delete` / `overwrite` / `rename` / `shutdown` always require explicit confirmation.
- Calculator parses an AST and refuses anything outside basic arithmetic.

---

## Keyboard shortcuts

| Shortcut       | Action                                  |
|----------------|-----------------------------------------|
| `Enter`        | Send the typed command                  |
| `Up` / `Down`  | Cycle through previous commands         |
| `Ctrl+,`       | Open Settings                           |
| `Ctrl+H`       | Hide JARVIS to background mode          |

---

## Build a standalone `.exe`

```powershell
python build_exe.py
```

Produces `dist/JARVIS.exe` (~150–250 MB; only `QtCore` / `QtGui` / `QtWidgets` are
bundled, heavy Qt modules are excluded). The first run recreates `data/` beside
the exe.

> Windows will prompt for microphone access on first launch — ensure
> **Settings → Privacy → Microphone → Desktop apps** is **On**.

---

## License

MIT — see [`LICENSE`](LICENSE).
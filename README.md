# JARVIS — Voice-Controlled Desktop Assistant

A modular, production-quality, **voice-controlled desktop assistant for Windows**, inspired
by Iron Man's J.A.R.V.I.S. It listens to your voice, understands intent, asks for permission
before doing anything sensitive, and speaks back. An optional **LLM brain** (Google **Gemini**
or any OpenAI-compatible provider) lets it hold a real conversation **and** translate
free-form, multilingual speech into structured commands.

> Built with safety as a first-class citizen — JARVIS will **never** touch
> `C:\Windows`, `C:\Program Files`, `System32`, or any other system-critical path.

---

## Features

- **Modern PySide6 dashboard** — drag-to-resize splitter so the history pane and the
  conversation pane share width exactly the way you want, panels lift on a soft drop
  shadow, transcripts fade-in as new replies arrive, and toasts gently fade in/out
  instead of popping. The pulsing status dot is mirrored on a floating overlay;
  circular **Mic** + **Send** buttons sit next to the input; **wrap-grid quick-action
  chips** stay readable on every width.
- **Live UI updates** — change your name (voice command, settings, or first-run prompt)
  and the user pill + window title update instantly, no restart.
- **Flexible wake word** — both _"hey JARVIS, what time is it?"_ and just _"JARVIS,
  what time is it?"_ work. Variants like _"hi jarvis"_, _"ok jarvis"_, _"yo jarvis"_
  are also accepted, and the matcher tolerates common mis-transcriptions
  (`jervis`, `jarvix`).
- **Barge-in** — interrupt JARVIS mid-sentence by simply saying **"Jarvis"** while he
  is replying. Speech is purged immediately and the mic drops straight into a
  follow-up listen. No second wake word needed.
- **Long, natural follow-up window** — after every command a **14-second** follow-up
  window stays open so you can ask follow-ups without re-saying the wake phrase, and
  the window re-arms with each new command.
- **Recent-folder memory** — JARVIS remembers the last folder he created or touched.
  Then you can just say _"open it"_, _"go there"_, _"show me that folder"_, or
  _"list it"_ — even in another language — and the right path is used. The last
  10 paths are kept in a ring buffer in `data/memory.json`.
- **LLM as primary command interpreter** — when an LLM is configured (Google **Gemini**
  via Google AI Studio, or any OpenAI-compatible provider), JARVIS sends most
  utterances through it first to get a structured intent back. This means:
  * You can speak in **any language** ("Urdu fasi mei batao kitne baje hain" or
    "abre el bloc de notas") and the LLM translates that into the right action.
  * Free-form phrasing works — you don't have to memorise rule patterns.
  * A small **fast-path** of trusted regex intents (time/date/joke/volume/show-hide-ui
    etc.) skips the API call so the common cases stay snappy and offline.
- **Two LLM backends** — Google Gemini (native `google-genai` SDK with `application/json`
  response mode for reliable parsing) **or** any OpenAI-compatible HTTP API
  (OpenAI, Groq, OpenRouter, Together, Ollama, LM Studio…). Pick one in
  Settings → LLM brain.
- **Background mode** — fully hide the window with _"hey JARVIS hide UI"_ (or `--hidden`).
  The window is removed from the taskbar entirely (not minimised) — recover it via the
  system-tray icon, the floating always-on-top overlay, or by saying _"show UI"_. The
  overlay only expands to show text when you wake or reply, then auto-collapses again,
  so it never crowds the screen.
- **Minimal Settings dialog** — two tabs (**LLM brain** + **Permissions**), single
  column each, generous whitespace, no decorative chrome. Minimum dialog size is
  720 × 780 — shrink it further and a scrollbar simply appears.
- **Three run modes** — GUI, background voice-only listener, or text-only REPL.
- **Voice in / voice out** — SpeechRecognition (Google / Whisper) + Windows SAPI5 / pyttsx3.
  The speech engine is rebuilt before every utterance, so JARVIS reliably speaks **every**
  reply (the previous "silent after the first reply" bug is gone).
- **Rule-based intent parser** — fast, offline, deterministic for trusted commands; also
  the fallback when the LLM is offline or returns nonsense.
- **Permission system** — per-category `yes / always / no / never`, persisted to disk.
- **Safety guards** — protected paths, command-injection prevention, mandatory confirmations
  for destructive actions, AST-based safe calculator (no `eval`).
- **File ops** — open, list, search, create, rename, copy, move, delete (with confirmation).
- **Whitelisted app launcher** — never spawns a shell.
- **Utilities** — time, date, jokes, math, screenshot, system info, volume control,
  open URL, web search.
- **Scheduler** — _"wait 10 minutes then restart"_, _"wait until chrome.exe closes, then…"_.
- **Logs + history** — daily rotation, 7-day retention.
- **PyInstaller-ready** — one-file `.exe` build script (with PySide6 + Gemini bundled).

---

## Quick start

### 1. Install Python 3.10 – 3.14 (tested on 3.14.0)

### 2. Install dependencies

Headless / minimal install (works on Python 3.14, voice via `sounddevice` instead of PyAudio):

```powershell
pip install --upgrade pip
pip install -r requirements-core.txt
```

Full install (adds optional PyAudio + offline Whisper for Python ≤ 3.12):

```powershell
pip install -r requirements.txt
```

(Optional) inside a virtualenv:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

> **PyAudio install fails on Python 3.13/3.14?** That's expected — we ship a `sounddevice`
> backend that takes over automatically. Voice in/out still works on the core install.
>
> **Whisper too heavy?** Comment it out in `requirements.txt` — JARVIS falls back to the
> free Google SpeechRecognition API.

### 3. Run

```powershell
# Modern PySide6 dashboard, mic on, wake-word required (the default)
python main.py

# Start hidden (background mode) — recover via tray icon or "hey jarvis show ui"
python main.py --hidden

# Text-only REPL — great for headless or remote sessions
python main.py --text-only

# Background voice-only listener (no window)
python main.py --voice-only

# Stream every utterance as a command (no wake phrase). Use sparingly.
python main.py --no-wake

# GUI without the microphone (typing only)
python main.py --no-mic

# Disable the LLM even when an API key is set
python main.py --no-llm
```

> **Tip:** Once running, press `Ctrl+,` to open Settings or `Ctrl+H` to hide JARVIS to
> background mode.

---

## Connecting an LLM (optional)

In the GUI: click **⚙ Settings** (or press `Ctrl+,`), open the **LLM brain** tab. Pick a
provider preset (Google Gemini, OpenAI, Groq, OpenRouter, Together, Ollama, LM Studio…),
paste your API key, and click **Test connection**, then **Save**. Settings are written
to a `.env` file in the project root (existing comments are preserved).

### Google Gemini (recommended)

Get a free API key at [Google AI Studio](https://aistudio.google.com/app/apikey),
then either pick the **Google Gemini** preset in the Settings dialog or add to `.env`:

```env
LLM_PROVIDER=gemini
GEMINI_API_KEY=AIza...your-key...
GEMINI_MODEL=gemini-2.5-flash
```

Gemini is called via the native `google-genai` SDK with `response_mime_type=
"application/json"`, so JARVIS gets reliable, structured intents back even for
free-form, multilingual speech.

### OpenAI-compatible providers

```env
LLM_PROVIDER=openai
OPENAI_API_KEY=sk-...your-key...
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_MODEL=gpt-4o-mini
```

### What the LLM does for you

When configured, JARVIS:

* uses the LLM as the **primary command interpreter** for everything that isn't a
  trusted fast-path intent — so it understands free-form phrasing in any language,
* keeps using fast hardcoded rules for common commands (time, date, joke, volume,
  show/hide UI, …) so trivia stays snappy and offline,
* speaks every reply in **English** for TTS clarity, even when you spoke another
  language,
* maintains a short rolling memory so follow-ups make sense ("open it" right after
  "create a folder named Reports in D drive").

The LLM is taught (via the system prompt) exactly which intents JARVIS supports, plus
the most recently touched folder for pronoun resolution.

---

## Example commands

(All examples assume voice mode and the default wake-word setting; in the typed input
the `hey jarvis` prefix is optional.)

| You say                                                 | What happens                                |
|---------------------------------------------------------|---------------------------------------------|
| "Hey JARVIS, what time is it" / "what is the time"      | Speaks the answer                           |
| "Hey JARVIS, what is the date" / "what day is it"       | Speaks the day + date                       |
| "Tell me a joke" / "make me laugh"                      | Built-in joke list                          |
| "What is 12 times 7" / "calculate 5 plus 7"             | Safe AST calculator                         |
| "System info" / "how is my pc doing"                    | CPU / RAM / OS / disk usage                 |
| "Take a screenshot" / "grab my screen"                  | Saved to `data/screenshots/`                |
| "Volume up" / "volume down" / "mute"                    | Multimedia keys                             |
| "Open google.com"                                       | Default browser                             |
| "Search the web for weather Lahore"                     | Google search in browser                    |
| "Open my Documents folder"                              | Explorer                                    |
| "List files in Downloads"                               | Reads filenames                             |
| "Search for resume.pdf in D drive"                      | Recursive file search                       |
| "Create a folder named Projects in Documents"           | `FILE_WRITE` permission, then creates       |
| "Create a file notes.txt with this text: buy milk"      | Writes the file                             |
| "Rename file old.txt to new.txt"                        | Confirms, then renames                      |
| "Copy report.pdf to Documents"                          | Confirms, copies                            |
| "Move report.pdf to Desktop"                            | Confirms, moves                             |
| "Launch Chrome" / "open notepad"                        | Whitelist + `APP_LAUNCH` perm               |
| "Wait 10 minutes then restart the PC"                   | Async timer + confirmation                  |
| "Wait until notepad.exe closes, then shut down"         | Polls process, confirms, shuts down         |
| "Open it" / "go there" / "show that folder"             | Opens the most recently touched folder      |
| "List it" / "what's in that folder"                     | Lists the most recently touched folder      |
| "Call me Tony" / "my name is Tony"                      | Updates the user pill + window title live   |
| **"Hide UI" / "go to background" / "minimize the app"** | Hides the window; overlay + tray remain    |
| **"Show UI" / "show the dashboard" / "wake up"**        | Brings the window back                      |
| "Hello" / "thanks" / "how are you" / "who are you"      | Friendly chat (no permissions)              |
| "Reset permissions"                                     | Wipes stored consents (or use Settings)     |
| "Help"                                                  | Lists capabilities aloud                    |
| "Goodbye" / "Exit"                                      | Quits                                       |

When the LLM is on, you can also chat: _"Who built the Burj Khalifa?"_, _"Suggest a
weeknight pasta recipe"_, etc.

### Wake-word, follow-ups & barge-in

The wake phrase is on **by default**. Both _"hey JARVIS"_ **and** plain _"JARVIS"_
work, plus variants `ok jarvis`, `hi jarvis`, `hello jarvis`, `yo jarvis` and common
mis-transcriptions (`jervis`, `jarvix`). Three natural patterns:

- **One breath:** _"Hey JARVIS, what time is it?"_ or _"Jarvis, what time is it?"_
  → wake stripped, command runs.
- **Two-step:** _"Hey JARVIS"_ / _"Jarvis"_ → JARVIS replies _"Yes?"_ → next phrase
  becomes the command.
- **Barge-in:** while JARVIS is speaking, say _"Jarvis"_ to **interrupt the reply**.
  The current speech is cut, the status pill shows "Listening…", and the next phrase
  is taken straight as a follow-up command. No second wake word needed.

After every command we open a **14-second follow-up window** where the next command
runs **without** needing the wake word again — and the window re-arms with each command.

### "Open it" / "go there" — recent-folder memory

JARVIS remembers the last folder he created or touched (last 10, ring buffer). So:

```
You: "Hey JARVIS, create a folder called Reports in D drive."
JARVIS: "Created D:\Reports."
You: "Open it."          ← resolved to D:\Reports
You: "List that folder." ← also D:\Reports
```

Works across languages too when the LLM brain is on.

---

## Permission model

| Category         | Examples                                       |
|------------------|------------------------------------------------|
| `FILE_READ`      | list, search, open folder                      |
| `FILE_WRITE`     | create, rename, copy, move, delete, screenshot |
| `APP_LAUNCH`     | launch Chrome / Notepad / CMD / open URL       |
| `SYSTEM_CONTROL` | shutdown, restart, sleep, lock, log off        |

On first use of a category JARVIS asks **yes / always / no / never**.
Saved to `data/permissions.json`. Manage them any time from **⚙ Settings → Permissions**
(per-category radio + master Reset all) or by saying _"reset permissions"_.

---

## Safety rules (hard-coded)

- Writes/deletes are refused for any path inside:
  - `C:\Windows`
  - `C:\Program Files`, `C:\Program Files (x86)`
  - `C:\ProgramData`
  - `System32`
- All file paths are normalised & validated before use.
- Apps launch via `subprocess` argv lists — **no `shell=True`**.
- `delete` / `overwrite` / `rename` / `shutdown` always require an explicit confirmation.
- The calculator parses an AST and refuses anything that isn't basic arithmetic — no
  `eval`, no name lookups, no function calls.

---

## Background mode

Run JARVIS as a hidden listener so it stays out of the way until you call it:

```powershell
python main.py --hidden
```

The window is `withdraw()`-n (no taskbar entry). A small **compact** overlay sits in
the top-right corner — just the pulsing dot + "JARVIS" label, ~140px wide, totally
unobtrusive. It only **expands** to show JARVIS' status and last reply when you
explicitly engage him (wake-word detected or a new spoken reply lands), then
auto-collapses back to compact after ~7 seconds. A tray icon (when `pystray` is
installed) also gives you **Show / Hide / Quit**. From any state, just say _"hey
JARVIS, show UI"_ to bring the window back. Press `Ctrl+H` while the window is
focused to send it to the background again.

---

## Keyboard shortcuts

| Shortcut       | Action                                  |
|----------------|-----------------------------------------|
| `Enter`        | Send the typed command                  |
| `Up` / `Down`  | Cycle through previous commands         |
| `Ctrl+,`       | Open Settings                           |
| `Ctrl+H`       | Hide JARVIS to background mode          |

---

## Configuration & data location

By default JARVIS keeps everything (`memory.json`, `permissions.json`,
`history.json`, rotated logs) in `data/` next to the executable / project root.
To relocate them — for example onto a synced drive or for a portable install —
set the `JARVIS_DATA_DIR` environment variable before launching:

```powershell
$env:JARVIS_DATA_DIR = "D:\Sync\JARVIS"
python main.py
```

The directory is created on first launch if it doesn't already exist.

---

## Build a standalone `.exe`

```powershell
python build_exe.py
```

This produces `dist/JARVIS.exe`. The build is configured to bundle only the
three Qt modules JARVIS uses (`QtCore`, `QtGui`, `QtWidgets`) and explicitly
excludes the heavy unused ones (Qt3D, QtWebEngine, QtMultimedia, QtBluetooth,
QtCharts, …) — that keeps the binary in the ~150–250 MB range instead of
500+ MB. The first run will recreate the `data/` folder beside the exe.

> Windows will prompt the user for microphone access on first launch. Make sure
> **Settings → Privacy → Microphone → Desktop apps** is **On**.

---

## License

MIT — do whatever you want, but don't blame me if you tell JARVIS to format `D:\` 🙃

# Screeny

Local voice-controlled **Windows desktop agent**. You talk or type a command; Screeny plans the task, opens apps or websites, and uses a vision model to click the UI when needed. Everything runs on your machine through [Ollama](https://ollama.com) — no cloud API keys.

This is the project I am submitting for **GDG SNU AI/ML**.

**Repo:** [https://github.com/aravarav0/screeny](https://github.com/aravarav0/screeny)

## What I planned

I wanted a small “Jarvis” for my own PC:

- Speak a goal in plain English (`install discord`, `open instagram on chrome`)
- Route simple actions through tools instead of screenshotting every time
- Use a **vision-language model** only when the screen actually has to be read
- Keep the whole pipeline **local** on an 8GB VRAM laptop
- Show progress in a floating overlay so you can see what the agent is doing

## What is actually implemented

| Piece | Status |
|-------|--------|
| Voice in (Whisper) + speech out (TTS) | Working |
| Text commands + floating CustomTkinter overlay | Working |
| Fast tools (launch app, open URL, Windows search) | Working |
| Planner LLM (`llama3.2:3b`) that returns structured JSON steps | Working |
| Vision loop (`qwen2.5vl:7b`) that screenshots and clicks | Working |
| Windows UI Automation path for native apps | Working |
| Install flow (search vendor page → download → run installer) | Working, still being hardened |
| OCR + vendor-page filters so it prefers a real **DOWNLOAD** button | Working |
| Safety: pyautogui failsafe (mouse to top-left), cancel / stop | Working |

**Stack:** Python 3.11, Ollama, Qwen2.5-VL, Llama 3.2, Whisper, RapidOCR, pyautogui, Windows UIA, CustomTkinter.

### How a command is handled

```
You (voice or text)
        │
        ▼
   Router  ── known apps / sites / social replies ──► tools (no GPU)
        │
        ▼
   Planner (llama3.2:3b)  → JSON: open URL, search, ask user, or hand off
        │
        ▼
   Vision loop (qwen2.5vl:7b)
        screenshot → understand screen → click / type / wait
        OCR + UI Automation as extra grounding
        ▼
   Overlay shows plan, actions, errors, install phase
```

Only one large model is loaded at a time so it fits in ~8GB VRAM.

## What I still want to add

- More reliable vendor-page clicks (installers still miss the wrong CTA sometimes)
- Global hotkey to expand/collapse the overlay without focusing it
- Settings panel that actually lists Ollama models and connection state
- Better Instagram / login-wall handling after a site is opened
- Tests around routing and the install state machine
- Optional smaller vision model for faster steps

## Setup

### 1. Ollama

Install from [ollama.com](https://ollama.com), then:

```powershell
ollama pull qwen2.5vl:7b
ollama pull llama3.2:3b
```

### 2. Python

```powershell
git clone https://github.com/aravarav0/screeny.git
cd screeny
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[voice,pointer]"
```

### 3. Run

```powershell
screeny
```

Other modes:

```powershell
screeny --no-ui      # terminal voice
screeny --text       # type instead of speaking
screeny open spotify # one-shot text command
```

Move the mouse to the **top-left corner** to abort a runaway click loop.

## Examples

| You say | What happens |
|---------|----------------|
| `open spotify` | Launches the app if installed |
| `install discord` | Search → official download page → DOWNLOAD → installer |
| `open instagram on chrome` | Opens Chrome to Instagram, then vision can continue |
| `open youtube and search lofi hip hop` | URL + search |
| `open notepad and type grocery list` | Vision + typing |

## Config

Copy `.env.example` to `.env` if you want to override defaults:

- `VISION_MODEL` — default `qwen2.5vl:7b`
- `PLANNER_MODEL` — default `llama3.2:3b`
- `MAX_VISION_STEPS` — default `25`
- `WHISPER_MODEL` — default `small`

Installers go to `data/downloads/`. Debug screenshots go to `data/debug/` (gitignored).

## Layout

```
screeny/
  main.py              entrypoint
  router.py            intent routing
  planner.py           LLM planner
  vision_loop.py       screenshot → act loop
  ui_grounding.py      click targets from UI / OCR
  tools.py             launch apps, open URLs
  ollama_client.py     local model calls
  ui_app.py            floating overlay
  ui/                  overlay widgets + theme
```

## License

MIT

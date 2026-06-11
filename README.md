# Screeny

Local Jarvis-style agent for Windows. Talk or type a command; Screeny opens apps instantly or uses vision to click around your screen. Everything runs on your machine via **Ollama**.

Built for laptops like yours: **RTX GPU with 8GB VRAM**, Ryzen 7, 16–32GB RAM.

## What it does

- **Fast tools** — "open Spotify", "open YouTube", "open chrome" without burning GPU cycles
- **Vision loop** — screenshot → Qwen2.5-VL → mouse/keyboard for anything on screen
- **Whisper `small` model** — better speech recognition (default; first run downloads ~460MB)
- Type commands in the **text box** if voice mishears you
- **Voice control** — talk to Screeny; it listens with local Whisper and replies with speech
- **Text mode** — `screeny --text` if you prefer typing
- **Safety** — move mouse to the **top-left corner** to abort (pyautogui failsafe)

## Setup

### 1. Ollama

Install from [ollama.com](https://ollama.com) if needed, then pull models:

```powershell
ollama pull qwen2.5vl:7b
ollama pull llama3.2:3b
```

Keep Ollama running (it usually starts with Windows). The planner uses a small 3B model; vision uses the 7B model — only one loads at a time (~6GB VRAM).

### 2. Python deps

```powershell
git clone https://github.com/aravarav0/screeny.git
cd screeny
python -m venv .venv
.venv\Scripts\activate
pip install -e .
```

Voice extras:

```powershell
pip install -e ".[voice]"
```

### 3. Run

```powershell
screeny
```

A small floating widget appears bottom-right. Flip the switch to **activate**, talk when it says **Listening**, and watch it expand when **Speaking**. Flip off to deactivate.

Other modes:

```powershell
screeny --no-ui      # terminal voice mode
screeny --text       # keyboard mode
```

Single command (text only):

```powershell
screeny open spotify
```

## Examples

| You say | What happens |
|---------|----------------|
| `download spotify` | Checks if installed, downloads official installer, runs setup |
| `install discord if I don't have it` | Same flow for Discord and many other apps |
| `open youtube and search lofi hip hop` | Opens YouTube search |
| `open my youtube analytics` | Opens YouTube Studio, then clicks Analytics if needed |
| `open notepad and type grocery list` | Vision + typing |

## Config

Copy `.env.example` to `.env` or set environment variables:

- `VISION_MODEL` — default `qwen2.5vl:7b`
- `PLANNER_MODEL` — default `llama3.2:3b` (understands intent before acting)
- `MAX_VISION_STEPS` — default `25`
- `VOICE_MODE` — default `1` (voice on when extras installed)
- `TTS_VOICE` — partial name match, e.g. `Zira` or `David`
- `TTS_RATE` — speech speed, default `175`
- `WHISPER_MODEL` — default `small` (use `base` for faster/weaker, `medium` for best quality)
- `MIC_DEVICE_INDEX` — optional mic number if the wrong device is used

## How it works

```
You → install flow (check installed → download official installer → run setup)
    → smart intents (YouTube Studio, Gmail, Google search…)
    → planner LLM (llama3.2:3b) thinks of URLs / searches / steps
    → fast tools (launch app, open URL, Windows search fallback)
    → vision loop only when UI clicks are still needed
```

Installers save to `data/downloads/`. Screenshots save to `data/screenshots/`.

## Tips

- Use **tools** for known apps; use **vision** for clicking UI.
- First vision step may take 10–20s while the model loads into VRAM.
- If clicks are slightly off, check Windows display scaling (100% works best).

## License

MIT

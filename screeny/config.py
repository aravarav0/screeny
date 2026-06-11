from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    ollama_host: str = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")
    vision_model: str = os.getenv("VISION_MODEL", "qwen2.5vl:7b")
    planner_model: str = os.getenv("PLANNER_MODEL", "llama3.2:3b")
    max_vision_steps: int = int(os.getenv("MAX_VISION_STEPS", "12"))
    max_vision_steps_install: int = int(os.getenv("MAX_VISION_STEPS_INSTALL", "20"))
    vision_stall_limit: int = int(os.getenv("VISION_STALL_LIMIT", "4"))
    # How many waits in a row are allowed (downloads/loads legitimately look static).
    vision_max_waits: int = int(os.getenv("VISION_MAX_WAITS", "4"))
    ollama_num_ctx: int = int(os.getenv("OLLAMA_NUM_CTX", "8192"))
    vision_max_width: int = int(os.getenv("VISION_MAX_WIDTH", "1280"))
    vision_jpeg_quality: int = int(os.getenv("VISION_JPEG_QUALITY", "72"))
    ollama_num_predict: int = int(os.getenv("OLLAMA_NUM_PREDICT", "160"))
    grid_max_width: int = int(os.getenv("GRID_MAX_WIDTH", "960"))
    grid_single_stage: bool = os.getenv("GRID_SINGLE_STAGE", "0") == "1"
    ocr_max_width: int = int(os.getenv("OCR_MAX_WIDTH", "1280"))
    action_pause: float = float(os.getenv("ACTION_PAUSE", "0.35"))
    page_load_wait: float = float(os.getenv("PAGE_LOAD_WAIT", "0.6"))
    post_click_delay: float = float(os.getenv("POST_CLICK_DELAY", "0.2"))
    installer_open_wait: float = float(os.getenv("INSTALLER_OPEN_WAIT", "1.0"))
    save_screenshots: bool = os.getenv("SAVE_SCREENSHOTS", "0") == "1"
    ollama_keep_alive: str = os.getenv("OLLAMA_KEEP_ALIVE", "10m")
    settings_page_wait: float = float(os.getenv("SETTINGS_PAGE_WAIT", "0.8"))
    planner_temperature: float = float(os.getenv("PLANNER_TEMPERATURE", "0.3"))
    vision_temperature: float = float(os.getenv("VISION_TEMPERATURE", "0.1"))
    refine_clicks: bool = os.getenv("REFINE_CLICKS", "1") == "1"
    refine_frac: float = float(os.getenv("REFINE_FRAC", "0.34"))
    use_ui_tree: bool = os.getenv("USE_UI_TREE", "1") == "1"
    ui_tree_budget: float = float(os.getenv("UI_TREE_BUDGET", "1.2"))
    text_only_min_elements: int = int(os.getenv("TEXT_ONLY_MIN_ELEMENTS", "6"))
    # UIA-first mode (inspired by Windows-Use): accessibility tree + text LLM, no screenshots.
    uia_first: bool = os.getenv("UIA_FIRST", "1") == "1"
    vision_fallback: bool = os.getenv("VISION_FALLBACK", "1") == "1"
    max_uia_steps: int = int(os.getenv("MAX_UIA_STEPS", "12"))
    uia_min_elements: int = int(os.getenv("UIA_MIN_ELEMENTS", "4"))
    use_uitars_fallback: bool = os.getenv("UITARS_FALLBACK", "1") == "1"
    uitars_model: str = os.getenv("UITARS_MODEL", "ui-tars")
    # "norm1000" (per UI-TARS docs), "abs" (pixels of sent image), or "auto".
    uitars_coord_mode: str = os.getenv("UITARS_COORD_MODE", "auto")
    cursor_animate: bool = os.getenv("CURSOR_ANIMATE", "1") == "1"
    cursor_move_duration: float = float(os.getenv("CURSOR_MOVE_DURATION", "0.35"))
    show_click_ring: bool = os.getenv("SHOW_CLICK_RING", "1") == "1"
    use_hybrid_pointer: bool = os.getenv("USE_HYBRID_POINTER", "1") == "1"
    use_grid_locator: bool = os.getenv("USE_GRID_LOCATOR", "1") == "1"
    screenshot_dir: Path = Path(os.getenv("SCREENSHOT_DIR", "data/screenshots"))
    download_dir: Path = Path(os.getenv("DOWNLOAD_DIR", "data/downloads"))
    whisper_model: str = os.getenv("WHISPER_MODEL", "small")
    whisper_language: str = os.getenv("WHISPER_LANGUAGE", "en")
    mic_device_index: int | None = (
        int(os.getenv("MIC_DEVICE_INDEX")) if os.getenv("MIC_DEVICE_INDEX") else None
    )
    tts_engine: str = os.getenv("TTS_ENGINE", "edge")  # edge (neural), sapi (offline robot)
    tts_rate: int = int(os.getenv("TTS_RATE", "175"))
    tts_volume: float = float(os.getenv("TTS_VOLUME", "1.0"))
    # Edge: en-US-JennyNeural, en-US-GuyNeural, en-GB-SoniaNeural …
    # SAPI: zira, david, aria (legacy Windows voices)
    tts_voice: str = os.getenv("TTS_VOICE", "en-US-JennyNeural")
    listen_timeout: float = float(os.getenv("LISTEN_TIMEOUT", "10"))
    phrase_limit: float = float(os.getenv("PHRASE_LIMIT", "20"))
    voice_mode: bool = os.getenv("VOICE_MODE", "1") == "1"


SETTINGS = Settings()

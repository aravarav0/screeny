"""Append-only session log for debugging (data/screeny.log)."""

from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path

from screeny.config import SETTINGS

_lock = threading.Lock()
_path = Path(SETTINGS.download_dir.parent) / "screeny.log"


def log(message: str) -> None:
    line = f"{datetime.now().isoformat(timespec='seconds')} {message}\n"
    try:
        _path.parent.mkdir(parents=True, exist_ok=True)
        with _lock:
            with _path.open("a", encoding="utf-8") as f:
                f.write(line)
    except OSError:
        pass
    print(message, flush=True)

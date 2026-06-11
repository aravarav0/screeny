from __future__ import annotations

import os
import re
import subprocess
import winreg
from functools import lru_cache
from pathlib import Path


PROGRAM_ROOTS = (
    Path(os.environ.get("ProgramFiles", r"C:\Program Files")),
    Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")),
    Path(os.environ.get("LOCALAPPDATA", "")),
)

START_MENU_DIRS = (
    Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
    Path(os.environ.get("PROGRAMDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
    Path(os.environ.get("USERPROFILE", "")) / "Desktop",
    Path(os.environ.get("PUBLIC", "")) / "Desktop",
)


def normalize_app_name(name: str) -> str:
    text = name.strip().lower()
    text = re.sub(r"^(my|the|a|an)\s+", "", text)
    text = re.sub(r"[^\w\s.-]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def find_application(name: str) -> Path | None:
    query = normalize_app_name(name)
    if not query:
        return None
    return _find_application_cached(query)


@lru_cache(maxsize=128)
def _find_application_cached(query: str) -> Path | None:
    exe_names = _exe_name_variants(query)

    for exe_name in exe_names:
        hit = _registry_app_path(exe_name)
        if hit:
            return hit

    hit = _find_start_menu_shortcut(query)
    if hit:
        return hit

    for exe_name in exe_names:
        hit = _find_on_path(exe_name)
        if hit:
            return hit

    hit = _find_in_program_folders(query, exe_names)
    if hit:
        return hit

    return _find_with_where(query)


def launch_application(name: str) -> tuple[bool, str, Path | None]:
    path = find_application(name)
    if not path:
        return False, f"Could not find '{name}' on this PC.", None

    try:
        os.startfile(path)  # noqa: S606 - intentional Windows app launch
    except OSError as exc:
        return False, f"Found {path.name} but could not launch it: {exc}", path

    label = path.stem if path.suffix.lower() == ".lnk" else path.name
    return True, f"Opened {label}.", path


def _exe_name_variants(query: str) -> tuple[str, ...]:
    base = query.replace(" ", "")
    spaced = query.replace(" ", "")
    title = "".join(part.capitalize() for part in query.split())
    names = {
        query,
        base,
        spaced,
        title,
        query.replace(" ", ""),
        f"{query}.exe",
        f"{base}.exe",
        f"{title}.exe",
    }
    return tuple(dict.fromkeys(n for n in names if n))


def _registry_app_path(exe_name: str) -> Path | None:
    if not exe_name.lower().endswith(".exe"):
        exe_name = f"{exe_name}.exe"

    key_roots = (
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"),
    )

    for hive, subkey in key_roots:
        try:
            with winreg.OpenKey(hive, subkey) as apps_key:
                with winreg.OpenKey(apps_key, exe_name) as app_key:
                    value, _ = winreg.QueryValueEx(app_key, None)
                    path = Path(str(value))
                    if path.exists():
                        return path
        except OSError:
            continue
    return None


def _find_on_path(exe_name: str) -> Path | None:
    if not exe_name.lower().endswith(".exe"):
        exe_name = f"{exe_name}.exe"
    for folder in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(folder) / exe_name
        if candidate.exists():
            return candidate
    return None


def _find_start_menu_shortcut(query: str) -> Path | None:
    best: tuple[int, Path] | None = None

    for root in START_MENU_DIRS:
        if not root.exists():
            continue
        for path in root.rglob("*.lnk"):
            score = _shortcut_score(query, path)
            if score <= 0:
                continue
            if best is None or score > best[0]:
                best = (score, path)

    return best[1] if best else None


def _shortcut_score(query: str, path: Path) -> int:
    stem = normalize_app_name(path.stem)
    parent = normalize_app_name(path.parent.name)

    if stem == query:
        return 100
    if parent == query:
        return 95
    if stem.startswith(query) or query.startswith(stem):
        return 85
    if query in stem or query in parent:
        return 75
    if all(part in stem for part in query.split()):
        return 65
    return 0


def _find_in_program_folders(query: str, exe_names: tuple[str, ...]) -> Path | None:
    normalized_names = {normalize_app_name(n.replace(".exe", "")) for n in exe_names}

    for root in PROGRAM_ROOTS:
        if not root.exists():
            continue

        for exe_name in exe_names:
            plain = exe_name.replace(".exe", "")
            direct = root / plain / exe_name
            if direct.exists():
                return direct
            direct = root / plain / f"{plain}.exe"
            if direct.exists():
                return direct

        try:
            for child in root.iterdir():
                if not child.is_dir():
                    continue
                child_name = normalize_app_name(child.name)
                if child_name not in normalized_names and query not in child_name:
                    continue
                for exe_name in exe_names:
                    candidate = child / exe_name
                    if candidate.exists():
                        return candidate
                    candidate = child / f"{exe_name.replace('.exe', '')}.exe"
                    if candidate.exists():
                        return candidate
        except OSError:
            continue

    return None


def _find_with_where(query: str) -> Path | None:
    for candidate in (query, f"{query}.exe"):
        try:
            result = subprocess.run(
                ["where", candidate],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode != 0:
            continue
        first = result.stdout.strip().splitlines()[0].strip()
        path = Path(first)
        if path.exists():
            return path
    return None

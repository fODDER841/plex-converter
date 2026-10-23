"""
Locate ffmpeg.exe and ffprobe.exe with a configurable search order:
  1. Configured directory (saved in settings)
  2. System PATH
  3. WinGet package directories  (%LOCALAPPDATA%\\Microsoft\\WinGet\\Packages\\*ffmpeg*)
  4. Common install locations
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


def _pair(directory: Path) -> tuple[str, str] | None:
    ffmpeg = directory / "ffmpeg.exe"
    ffprobe = directory / "ffprobe.exe"
    if ffmpeg.exists() and ffprobe.exists():
        return str(ffmpeg), str(ffprobe)
    return None


def _search_winget() -> tuple[str, str] | None:
    base = Path(os.environ.get("LOCALAPPDATA", "C:\\")) / "Microsoft" / "WinGet" / "Packages"
    if not base.exists():
        return None
    for pkg_dir in base.iterdir():
        if "ffmpeg" not in pkg_dir.name.lower():
            continue
        # Walk into version subdirs looking for bin/ffmpeg.exe
        for exe in pkg_dir.rglob("ffmpeg.exe"):
            ffprobe = exe.parent / "ffprobe.exe"
            if ffprobe.exists():
                return str(exe), str(ffprobe)
    return None


_COMMON_DIRS: list[Path] = [
    Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "ffmpeg" / "bin",
    Path(r"C:\ffmpeg\bin"),
    Path(r"C:\ffmpeg"),
]


def resolve(configured_dir: str | None = None) -> tuple[str | None, str | None, str]:
    """
    Return (ffmpeg_exe, ffprobe_exe, source_description).
    All three are returned even on failure — source_description will be "not found".
    """
    # 1. Configured directory
    if configured_dir:
        pair = _pair(Path(configured_dir))
        if pair:
            return pair[0], pair[1], f"configured ({configured_dir})"

    # 2. PATH
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg and ffprobe:
        return ffmpeg, ffprobe, f"PATH ({Path(ffmpeg).parent})"

    # 3. WinGet packages
    result = _search_winget()
    if result:
        return result[0], result[1], f"WinGet ({Path(result[0]).parent})"

    # 4. Common install locations
    for d in _COMMON_DIRS:
        pair = _pair(d)
        if pair:
            return pair[0], pair[1], str(d)

    return None, None, "not found"


def get_version(exe: str) -> str:
    """Return the first line of `exe -version`, e.g. 'ffmpeg version 9.0.2-full_build'."""
    try:
        result = subprocess.run(
            [exe, "-version"],
            capture_output=True, text=True, timeout=10,
        )
        first = result.stdout.split("\n")[0].strip()
        return first if first else "(version unknown)"
    except Exception as exc:
        return f"(error: {exc})"

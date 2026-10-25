"""
Locate ffmpeg.exe and ffprobe.exe with a configurable search order:
  1. Configured directory (saved in settings)
  2. System PATH
  3. WinGet package directories  (%LOCALAPPDATA% and %PROGRAMFILES% \\WinGet\\Packages\\*ffmpeg*)
  4. Common install locations
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def _pair(directory: Path) -> tuple[str, str] | None:
    ffmpeg = directory / "ffmpeg.exe"
    ffprobe = directory / "ffprobe.exe"
    if ffmpeg.exists() and ffprobe.exists():
        return str(ffmpeg), str(ffprobe)
    return None


def _winget_package_dirs() -> list[Path]:
    # User-scope installs (the default) and machine-scope installs.
    return [
        Path(os.environ.get("LOCALAPPDATA", "C:\\")) / "Microsoft" / "WinGet" / "Packages",
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "WinGet" / "Packages",
    ]


def _search_winget() -> tuple[str, str] | None:
    for base in _winget_package_dirs():
        if not base.exists():
            continue
        for pkg_dir in base.iterdir():
            if "ffmpeg" not in pkg_dir.name.lower():
                continue
            # Walk into version subdirs looking for bin/ffmpeg.exe
            for exe in pkg_dir.rglob("ffmpeg.exe"):
                ffprobe = exe.parent / "ffprobe.exe"
                if ffprobe.exists():
                    return str(exe), str(ffprobe)
    return None


def find_in_folder(folder: str) -> str | None:
    """
    Return the directory holding ffmpeg.exe + ffprobe.exe within a user-picked
    folder: the folder itself, its bin/, or anywhere up to two levels below.
    """
    root = Path(folder)
    for d in (root, root / "bin"):
        if _pair(d):
            return str(d)
    for pattern in ("*/ffmpeg.exe", "*/*/ffmpeg.exe"):
        for exe in root.glob(pattern):
            if _pair(exe.parent):
                return str(exe.parent)
    return None


def refresh_path_from_registry() -> None:
    """
    Reload PATH from the registry so changes made by an installer after this
    process started become visible without restarting the app.
    """
    if sys.platform != "win32":
        return
    import winreg

    parts: list[str] = []
    for hive, key in (
        (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
        (winreg.HKEY_CURRENT_USER, "Environment"),
    ):
        try:
            with winreg.OpenKey(hive, key) as k:
                value, _ = winreg.QueryValueEx(k, "Path")
                parts.append(os.path.expandvars(value))
        except OSError:
            continue
    if parts:
        os.environ["PATH"] = ";".join(parts)


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
            creationflags=_NO_WINDOW,
        )
        first = result.stdout.split("\n")[0].strip()
        return first if first else "(version unknown)"
    except Exception as exc:
        return f"(error: {exc})"

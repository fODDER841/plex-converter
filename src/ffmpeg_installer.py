"""
Install FFmpeg via winget (package Gyan.FFmpeg) with streamed progress.

winget's exit code is not a reliable success signal (e.g. "already installed"
returns 0x8A15002B), so callers should re-run ffmpeg_finder.resolve() after
install() returns and treat "found" as success.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

PACKAGE_ID = "Gyan.FFmpeg"
INSTALL_ARGS = [
    "install", "--id", PACKAGE_ID,
    "--accept-source-agreements", "--accept-package-agreements",
]

# Shown when winget is unavailable (older Windows 10 builds without App Installer).
APP_INSTALLER_URL = "https://apps.microsoft.com/detail/9nblggh4nns1"
# Direct .zip (Windows can unzip natively, unlike the .7z builds).
FFMPEG_DOWNLOAD_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"

_SIZE_RE = re.compile(
    r"([\d.]+)\s*(KB|MB|GB)\s*/\s*([\d.]+)\s*(KB|MB|GB)", re.IGNORECASE
)
_PCT_RE = re.compile(r"(\d{1,3})\s*%")
_UNITS = {"KB": 1 / 1024, "MB": 1.0, "GB": 1024.0}


@dataclass
class Progress:
    stage: str                    # "downloading" | "installing" | "working"
    fraction: float | None = None  # 0..1 when known
    detail: str = ""              # e.g. "12 MB of 80 MB"


@dataclass
class InstallResult:
    returncode: int
    output: str


def find_winget() -> str | None:
    """Return a working winget executable, or None if winget isn't usable."""
    candidates = [shutil.which("winget")]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(str(Path(local) / "Microsoft" / "WindowsApps" / "winget.exe"))
    for exe in candidates:
        if not exe or not os.path.exists(exe):
            continue
        try:
            r = subprocess.run(
                [exe, "--version"], capture_output=True, timeout=15,
                stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW,
            )
            if r.returncode == 0:
                return exe
        except (OSError, subprocess.TimeoutExpired):
            continue
    return None


def parse_progress(line: str) -> Progress | None:
    """Translate one line of winget output into a Progress, or None if not meaningful."""
    m = _SIZE_RE.search(line)
    if m:
        done = float(m.group(1)) * _UNITS[m.group(2).upper()]
        total = float(m.group(3)) * _UNITS[m.group(4).upper()]
        frac = min(done / total, 1.0) if total > 0 else None
        return Progress("downloading", frac, f"{done:.0f} MB of {total:.0f} MB")
    low = line.lower()
    if "downloading" in low:
        return Progress("downloading")
    if any(k in low for k in ("installing", "extracting", "starting package install")):
        return Progress("installing")
    m = _PCT_RE.search(line)
    if m:
        return Progress("working", min(int(m.group(1)), 100) / 100)
    return None


def _split_lines(buf: str) -> tuple[list[str], str]:
    """Split on \\r or \\n (winget redraws progress with \\r); return (complete, remainder)."""
    parts = re.split(r"[\r\n]", buf)
    return parts[:-1], parts[-1]


def _clean(line: str) -> str:
    # Drop spinner backspaces and box-drawing progress bar glyphs.
    line = re.sub(r".\x08", "", line).replace("\x08", "")
    line = re.sub(r"[▀-▟]", "", line)
    return line.strip(" -\\|/\t")


def install(
    winget_exe: str,
    on_progress: Callable[[Progress], None] | None = None,
) -> InstallResult:
    """Run the winget install synchronously (call from a worker thread)."""
    log: list[str] = []
    buf = ""
    with subprocess.Popen(
        [winget_exe, *INSTALL_ARGS],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW,
    ) as proc:
        assert proc.stdout is not None
        while True:
            chunk = proc.stdout.read1(4096)
            if not chunk:
                break
            buf += chunk.decode("utf-8", errors="replace")
            lines, buf = _split_lines(buf)
            for raw in lines:
                line = _clean(raw)
                if not line:
                    continue
                # Byte-count redraws would flood the log; keep only real messages.
                if not _SIZE_RE.search(line) and (not log or log[-1] != line):
                    log.append(line)
                if on_progress:
                    p = parse_progress(line)
                    if p:
                        on_progress(p)
        if buf.strip():
            log.append(_clean(buf))
    return InstallResult(proc.returncode, "\n".join(log))

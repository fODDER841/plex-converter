import os
import subprocess
import sys
import threading
from typing import Callable

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

from .probe import VideoInfo


def build_cmd(
    input_path: str, output_path: str, info: VideoInfo, ffmpeg_exe: str = "ffmpeg"
) -> list[str]:
    cmd = [ffmpeg_exe, "-y", "-i", input_path]

    if info.needs_reencode:
        # Cap at 1080p; keep original resolution if smaller.
        # scale=-2:min(ih\,1080) preserves aspect ratio and ensures even width.
        cmd += [
            "-c:v", "libx264",
            "-crf", "18",
            "-preset", "medium",
            "-vf", r"scale=-2:min(ih\,1080)",
            "-pix_fmt", "yuv420p",
        ]
    else:
        cmd += ["-c:v", "copy"]

    cmd += ["-c:a", "copy", "-c:s", "copy"]

    # Structured progress to stdout; suppress per-frame stats on stderr.
    cmd += ["-progress", "pipe:1", "-nostats"]

    cmd.append(output_path)
    return cmd


def convert(
    input_path: str,
    output_path: str,
    info: VideoInfo,
    ffmpeg_exe: str = "ffmpeg",
    progress_callback: Callable[[float], None] = lambda _: None,
    cancel_event: threading.Event | None = None,
) -> None:
    if cancel_event is None:
        cancel_event = threading.Event()
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    cmd = build_cmd(input_path, output_path, info, ffmpeg_exe)

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            encoding="utf-8",
            errors="replace",
            creationflags=_NO_WINDOW,
        )
    except FileNotFoundError:
        raise RuntimeError(
            f"ffmpeg not found at: {ffmpeg_exe}\n"
            "Use the FFmpeg Settings button to locate the binaries."
        )

    # Drain stderr in a background thread so it never blocks the main pipe.
    stderr_lines: list[str] = []

    def _drain_stderr() -> None:
        for line in proc.stderr:
            stderr_lines.append(line)

    stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
    stderr_thread.start()

    total_us = info.duration_seconds * 1_000_000

    for raw_line in proc.stdout:
        if cancel_event.is_set():
            proc.kill()
            raise RuntimeError("Conversion cancelled.")

        line = raw_line.strip()
        if line.startswith("out_time_us="):
            try:
                elapsed_us = int(line.split("=", 1)[1])
                if total_us > 0:
                    progress_callback(min(elapsed_us / total_us, 1.0))
            except (ValueError, IndexError):
                pass

    proc.wait()
    stderr_thread.join(timeout=3)

    if proc.returncode != 0:
        tail = "".join(stderr_lines[-30:])
        raise RuntimeError(
            f"FFmpeg exited with code {proc.returncode}.\n\n{tail}"
        )

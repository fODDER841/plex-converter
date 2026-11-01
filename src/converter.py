import os
import signal
import subprocess
import sys
import threading
import time
from typing import Callable, Optional

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

from .probe import StreamInfo, VideoInfo, probe

# Suffix for in-progress output. The final name only appears once ffmpeg has
# finished successfully, so an existing output file is always complete.
PART_SUFFIX = ".part"
# Temporary subtitle file used when converting DivX (xsub) subtitles.
SUBS_SUFFIX = ".subs.mkv"

# ---------------------------------------------------------------------------
# Subtitle handling. MKV can't store every subtitle codec, so each subtitle
# stream is copied, converted, or (if impossible) the file is refused rather
# than silently losing the track.
# ---------------------------------------------------------------------------

_SUB_COPY = frozenset({
    "subrip", "srt", "ass", "ssa", "webvtt", "text",
    "hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "hdmv_text_subtitle",
    "arib_caption",
})
# Text formats MKV can't hold; converted to SRT.
_SUB_TO_SRT = frozenset({
    "mov_text", "microdvd", "subviewer", "subviewer1", "sami", "realtext",
    "jacosub", "mpl2", "pjs", "vplayer", "stl", "eia_608",
})
# DivX bitmap subtitles (common in AVI). Converted to VobSub via an
# intermediate DVB step: ffmpeg drops xsub's timing offset when encoding
# straight to VobSub, but keeps it when encoding to DVB.
_SUB_VIA_DVB = frozenset({"xsub"})
_SUB_UNSUPPORTED = frozenset({"dvb_teletext"})


def subtitle_action(codec: str) -> str:
    if codec in _SUB_TO_SRT:
        return "srt"
    if codec in _SUB_VIA_DVB:
        return "vobsub"
    if codec in _SUB_UNSUPPORTED:
        return "unsupported"
    return "copy"  # known-good, or unknown: let ffmpeg try


def unsupported_streams(info: VideoInfo) -> list[StreamInfo]:
    return [s for s in info.streams
            if s.kind == "subtitle" and subtitle_action(s.codec) == "unsupported"]


def dropped_streams(info: VideoInfo) -> list[StreamInfo]:
    """Streams deliberately not carried over (cover-art images, data tracks)."""
    return [s for s in info.streams
            if s.attached_pic or s.kind not in ("video", "audio", "subtitle", "attachment")]


def _needs_subs_prestep(info: VideoInfo) -> bool:
    return any(s.kind == "subtitle" and subtitle_action(s.codec) == "vobsub"
               for s in info.streams)


class ConversionCancelled(RuntimeError):
    def __init__(self) -> None:
        super().__init__("Conversion cancelled.")


def video_args(info: VideoInfo) -> list[str]:
    if info.needs_reencode:
        # Cap at 1080p; keep original resolution if smaller.
        # scale=-2:min(ih\,1080) preserves aspect ratio and ensures even width.
        return [
            "-c:v", "libx264",
            "-crf", "18",
            "-preset", "medium",
            "-vf", r"scale=-2:min(ih\,1080)",
            "-pix_fmt", "yuv420p",
        ]
    return ["-c:v", "copy"]


def _input_args(input_path: str) -> list[str]:
    args = []
    if input_path.lower().endswith(".avi"):
        # AVI often lacks per-packet timestamps, which MKV muxing needs.
        args += ["-fflags", "+genpts"]
    return args + ["-i", input_path]


def _output_args(output_path: str) -> list[str]:
    # Structured progress to stdout; suppress per-frame stats on stderr.
    args = ["-progress", "pipe:1", "-nostats"]
    if not output_path.lower().endswith(".mkv"):
        args += ["-f", "matroska"]  # can't infer the format from ".part"
    return args + [output_path]


def build_subs_cmd(input_path: str, subs_path: str, info: VideoInfo,
                   ffmpeg_exe: str = "ffmpeg") -> list[str]:
    """Pre-step: xsub streams -> DVB subtitles in a temporary file."""
    cmd = [ffmpeg_exe, "-y", *_input_args(input_path)]
    for s in info.streams:
        if s.kind == "subtitle" and subtitle_action(s.codec) == "vobsub":
            cmd += ["-map", f"0:{s.index}"]
    return cmd + ["-c:s", "dvb_subtitle", *_output_args(subs_path)]


def build_cmd(
    input_path: str,
    output_path: str,
    info: VideoInfo,
    ffmpeg_exe: str = "ffmpeg",
    subs_path: Optional[str] = None,
) -> list[str]:
    cmd = [ffmpeg_exe, "-y", *_input_args(input_path)]
    if subs_path:
        cmd += ["-i", subs_path]

    sub_codecs: list[str] = []
    if info.streams:
        # Every real video, audio and subtitle track, plus attachments (e.g.
        # fonts for styled subtitles), in their original order.
        maps: list[str] = []
        dvb_n = 0
        for s in info.streams:
            if s.attached_pic:
                continue
            if s.kind in ("video", "audio", "attachment"):
                maps += ["-map", f"0:{s.index}"]
            elif s.kind == "subtitle":
                action = subtitle_action(s.codec)
                if action == "vobsub" and subs_path:
                    maps += ["-map", f"1:s:{dvb_n}"]
                    dvb_n += 1
                    sub_codecs.append("dvd_subtitle")
                else:
                    maps += ["-map", f"0:{s.index}"]
                    sub_codecs.append("srt" if action == "srt" else "copy")
        cmd += maps
    else:
        cmd += ["-map", "0:V?", "-map", "0:a?", "-map", "0:s?", "-map", "0:t?"]

    cmd += video_args(info)
    cmd += ["-c:a", "copy", "-c:s", "copy", "-c:t", "copy"]
    for n, codec in enumerate(sub_codecs):
        if codec != "copy":
            cmd += [f"-c:s:{n}", codec]

    return cmd + _output_args(output_path)


# ---------------------------------------------------------------------------
# Process suspend/resume (used for pause)
# ---------------------------------------------------------------------------

def _suspend(proc: subprocess.Popen) -> None:
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.ntdll.NtSuspendProcess(int(proc._handle))
    else:
        os.kill(proc.pid, signal.SIGSTOP)


def _resume(proc: subprocess.Popen) -> None:
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.ntdll.NtResumeProcess(int(proc._handle))
    else:
        os.kill(proc.pid, signal.SIGCONT)


def _remove_quietly(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _run_ffmpeg(
    cmd: list[str],
    ffmpeg_exe: str,
    total_us: float,
    progress_callback: Callable[[float], None],
    cancel_event: threading.Event,
    pause_event: threading.Event,
    stall_timeout: float,
) -> None:
    """
    Run one ffmpeg command. Raises ConversionCancelled when cancelled and
    RuntimeError on failure or stall. Pausing suspends the process.
    """
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

    # Watchdog: handles cancel, pause/resume and stalls. The stdout loop below
    # blocks while ffmpeg is suspended, so these can't be checked there.
    last_activity = [time.monotonic()]
    outcome = {"cancelled": False, "stalled": False}
    finished = threading.Event()

    def _watch() -> None:
        suspended = False
        while not finished.wait(0.25):
            if cancel_event.is_set():
                outcome["cancelled"] = True
                proc.kill()  # works on a suspended process too
                return
            if pause_event.is_set() and not suspended:
                _suspend(proc)
                suspended = True
            elif not pause_event.is_set() and suspended:
                _resume(proc)
                suspended = False
                last_activity[0] = time.monotonic()
            if suspended:
                last_activity[0] = time.monotonic()
            elif time.monotonic() - last_activity[0] > stall_timeout:
                outcome["stalled"] = True
                proc.kill()
                return

    watcher = threading.Thread(target=_watch, daemon=True)
    watcher.start()

    try:
        for raw_line in proc.stdout:
            last_activity[0] = time.monotonic()
            line = raw_line.strip()
            if line.startswith("out_time_us="):
                try:
                    elapsed_us = int(line.split("=", 1)[1])
                    if total_us > 0:
                        progress_callback(min(elapsed_us / total_us, 1.0))
                except (ValueError, IndexError):
                    pass
        proc.wait()
    finally:
        finished.set()
        watcher.join(timeout=2)
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        stderr_thread.join(timeout=3)
        for pipe in (proc.stdout, proc.stderr):
            try:
                pipe.close()
            except OSError:
                pass

    if outcome["cancelled"] or (cancel_event.is_set() and proc.returncode != 0):
        raise ConversionCancelled()

    if outcome["stalled"]:
        tail = "".join(stderr_lines[-15:])
        raise RuntimeError(
            f"FFmpeg made no progress for {int(stall_timeout)} seconds and was stopped.\n\n{tail}"
        )

    if proc.returncode != 0:
        tail = "".join(stderr_lines[-30:])
        raise RuntimeError(
            f"FFmpeg exited with code {proc.returncode}.\n\n{tail}"
        )


def _verify_tracks(path: str, info: VideoInfo, ffprobe_exe: str) -> None:
    """Fail if the output lost any audio or subtitle track."""
    try:
        out = probe(path, ffprobe_exe=ffprobe_exe)
    except Exception as exc:
        raise RuntimeError(f"Converted file could not be read back: {exc}")
    expected = {k: info.count(k) for k in ("audio", "subtitle")}
    got = {k: out.count(k) for k in ("audio", "subtitle")}
    if got != expected or out.count("video") < 1:
        raise RuntimeError(
            "Converted file is missing tracks (expected "
            f"{expected['audio']} audio / {expected['subtitle']} subtitle, got "
            f"{got['audio']} audio / {got['subtitle']} subtitle). Not saved."
        )


def convert(
    input_path: str,
    output_path: str,
    info: VideoInfo,
    ffmpeg_exe: str = "ffmpeg",
    progress_callback: Callable[[float], None] = lambda _: None,
    cancel_event: threading.Event | None = None,
    pause_event: threading.Event | None = None,
    stall_timeout: float = 600.0,
    ffprobe_exe: Optional[str] = None,
) -> None:
    """
    Convert to `output_path` via a temporary `.part` file.

    All video, audio and subtitle tracks are kept. Setting `cancel_event`
    kills ffmpeg (even while paused). While `pause_event` is set the ffmpeg
    process is suspended. If ffmpeg produces no progress for `stall_timeout`
    seconds (not counting pauses) it is killed and the conversion fails.
    With `ffprobe_exe`, the result is checked for missing tracks before it
    is given its final name.
    """
    if cancel_event is None:
        cancel_event = threading.Event()
    if pause_event is None:
        pause_event = threading.Event()

    bad = unsupported_streams(info)
    if bad:
        raise RuntimeError(
            "This file has subtitle tracks MKV can't store ("
            + ", ".join(s.label for s in bad)
            + "). Not converted, so no track is lost."
        )

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    part_path = output_path + PART_SUFFIX
    subs_path = output_path + SUBS_SUFFIX if _needs_subs_prestep(info) else None
    total_us = info.duration_seconds * 1_000_000
    run = lambda cmd, cb: _run_ffmpeg(cmd, ffmpeg_exe, total_us, cb, cancel_event,
                                      pause_event, stall_timeout)

    try:
        if subs_path:
            run(build_subs_cmd(input_path, subs_path, info, ffmpeg_exe), lambda _f: None)
        run(build_cmd(input_path, part_path, info, ffmpeg_exe, subs_path), progress_callback)
        if ffprobe_exe:
            _verify_tracks(part_path, info, ffprobe_exe)
        try:
            os.replace(part_path, output_path)
        except OSError as exc:
            raise RuntimeError(f"Could not move finished file into place: {exc}")
    except BaseException:
        _remove_quietly(part_path)
        raise
    finally:
        if subs_path:
            _remove_quietly(subs_path)

"""Batch conversion: collect files, plan, estimate, run one at a time, report."""
from __future__ import annotations

import ctypes
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from .converter import (PART_SUFFIX, SUBS_SUFFIX, ConversionCancelled, build_cmd,
                        build_subs_cmd, convert, dropped_streams, subtitle_action,
                        unsupported_streams, video_args)
from .naming import (EpisodeGuess, episode_tag, flat_output_path, parse_episode,
                     plex_tv_path)
from .probe import VideoInfo, probe

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

VIDEO_EXTENSIONS = (
    ".mp4", ".mkv", ".avi", ".mov", ".wmv",
    ".m2ts", ".ts", ".m4v", ".flv", ".vob", ".mpg", ".mpeg",
)

# Scene "sample" clips would collide with the real episode.
_SAMPLE = re.compile(r"(?<![a-z])sample(?![a-z])", re.IGNORECASE)

QUEUED, CONVERTING, DONE, SKIPPED, FAILED = "queued", "converting", "done", "skipped", "failed"


def _natural_key(s: str) -> list:
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def collect_videos(paths: list[str]) -> list[tuple[str, str]]:
    """
    Expand dropped files/folders into (video_path, root) pairs. Folders are
    searched recursively; `root` is the dropped folder (or the file's parent),
    used as context when parsing show and season names.
    """
    out: list[tuple[str, str]] = []
    seen: set[str] = set()

    def _add(path: str, root: str) -> None:
        key = os.path.normcase(os.path.abspath(path))
        if key in seen:
            return
        if Path(path).suffix.lower() not in VIDEO_EXTENSIONS:
            return
        if _SAMPLE.search(Path(path).stem):
            return
        seen.add(key)
        out.append((os.path.abspath(path), root))

    for p in paths:
        if os.path.isdir(p):
            found: list[str] = []
            for dirpath, dirnames, filenames in os.walk(p):
                dirnames[:] = [d for d in dirnames if not _SAMPLE.fullmatch(d)]
                found += [os.path.join(dirpath, f) for f in filenames]
            for f in sorted(found, key=lambda f: _natural_key(os.path.relpath(f, p))):
                _add(f, os.path.abspath(p))
        elif os.path.isfile(p):
            _add(p, os.path.dirname(os.path.abspath(p)))
    return out


# ---------------------------------------------------------------------------
# Items and planning
# ---------------------------------------------------------------------------

@dataclass
class BatchItem:
    path: str
    root: str
    guess: EpisodeGuess
    season: Optional[int] = None
    episode: Optional[int] = None
    episode_end: Optional[int] = None
    info: Optional[VideoInfo] = None
    probe_error: Optional[str] = None
    status: str = QUEUED
    reason: str = ""          # why skipped/failed, or what was done
    output_path: str = ""
    elapsed: float = 0.0      # active conversion seconds
    confirmed: bool = False   # user checked/edited the episode numbers

    @classmethod
    def from_path(cls, path: str, root: str) -> "BatchItem":
        g = parse_episode(path, root)
        return cls(path=path, root=root, guess=g, season=g.season,
                   episode=g.episode, episode_end=g.episode_end)

    @property
    def display_name(self) -> str:
        try:
            return os.path.relpath(self.path, self.root)
        except ValueError:
            return os.path.basename(self.path)

    @property
    def tag(self) -> str:
        if self.season is None or self.episode is None:
            return ""
        return episode_tag(self.season, self.episode, self.episode_end)


@dataclass
class Plan:
    action: str               # "reencode" | "remux" | "skip" | "fail" | "probing"
    note: str = ""            # shown in the list
    needs_attention: bool = False


def _inside(path: str, folder: str) -> bool:
    try:
        return os.path.commonpath(
            [os.path.normcase(os.path.abspath(path)), os.path.normcase(os.path.abspath(folder))]
        ) == os.path.normcase(os.path.abspath(folder))
    except ValueError:  # different drives
        return False


def plan_items(
    items: list[BatchItem], show: str, year: Optional[str], output_dir: str,
    keep_names: bool = False,
) -> dict[int, Plan]:
    """
    Work out what will happen to each item, keyed by id(item). With
    `keep_names`, each file keeps its own name (extension changed to .mkv)
    and goes straight into `output_dir`; nothing is parsed or renamed.
    """
    plans: dict[int, Plan] = {}
    targets: dict[str, list[BatchItem]] = {}

    for it in items:
        it.output_path = ""
        if keep_names and output_dir:
            it.output_path = flat_output_path(it.path, output_dir)
        elif it.season is not None and it.episode is not None and show and output_dir:
            it.output_path = plex_tv_path(show, year, it.season, it.episode,
                                          output_dir, it.episode_end)
        if it.output_path:
            targets.setdefault(os.path.normcase(it.output_path), []).append(it)

    for it in items:
        if it.status != QUEUED:
            plans[id(it)] = Plan("skip" if it.status == SKIPPED else it.status, it.reason)
            continue
        if it.probe_error:
            plans[id(it)] = Plan("fail", f"Can't read file: {it.probe_error}", True)
        elif output_dir and _inside(it.path, output_dir):
            plans[id(it)] = Plan("skip", "Inside the output folder")
        elif not keep_names and (it.season is None or it.episode is None):
            plans[id(it)] = Plan("skip", "Couldn't find season/episode — double-click to set", True)
        elif not keep_names and not show:
            plans[id(it)] = Plan("skip", "Set the show title", True)
        elif len(targets.get(os.path.normcase(it.output_path), [])) > 1:
            other = next(o for o in targets[os.path.normcase(it.output_path)] if o is not it)
            what = "output name" if keep_names else "episode"
            plans[id(it)] = Plan(
                "skip", f"Same {what} as {other.display_name}", True)
        elif it.output_path and os.path.exists(it.output_path):
            plans[id(it)] = Plan("skip", "Already converted")
        elif it.info is None:
            plans[id(it)] = Plan("probing", "Inspecting…")
        elif unsupported_streams(it.info):
            bad = ", ".join(s.label for s in unsupported_streams(it.info))
            plans[id(it)] = Plan("fail", f"Has subtitles MKV can't store ({bad})", True)
        else:
            note = ("Check episode number (guessed)"
                    if it.guess.uncertain and not it.confirmed and not keep_names else "")
            plans[id(it)] = Plan("reencode" if it.info.needs_reencode else "remux",
                                 note, bool(note))
    return plans


def most_common(values: list[Optional[str]]) -> str:
    vals = [v for v in values if v]
    return Counter(vals).most_common(1)[0][0] if vals else ""


# ---------------------------------------------------------------------------
# Time estimate
# ---------------------------------------------------------------------------

@dataclass
class Rates:
    # Encoded output pixels per second (width*height*frames / wall seconds).
    encode_px_per_sec: float
    # Remux throughput in bytes per second (mostly disk-bound).
    remux_bytes_per_sec: float
    source: str = "default"   # "default" | "test encode" | "previous conversions"


def default_rates() -> Rates:
    cores = os.cpu_count() or 4
    # Rough libx264 -preset medium figure: ~6.5 Mpx/s per core.
    return Rates(encode_px_per_sec=6.5e6 * cores, remux_bytes_per_sec=80e6)


def _output_pixels(info: VideoInfo) -> float:
    h = info.height or 480
    w = info.width or 640
    if h > 1080:
        w, h = w * 1080 / h, 1080
    return w * h


def estimate_item(info: VideoInfo, rates: Rates) -> float:
    if info.needs_reencode:
        frames = info.duration_seconds * (info.fps or 25.0)
        return 2.0 + frames * _output_pixels(info) / max(rates.encode_px_per_sec, 1.0)
    return 2.0 + info.size_bytes / max(rates.remux_bytes_per_sec, 1.0)


def benchmark_encode(path: str, info: VideoInfo, ffmpeg_exe: str,
                     sample_seconds: float = 15.0) -> Optional[float]:
    """Encode a short sample to /dev/null; return output pixels per second."""
    start_at = info.duration_seconds / 3 if info.duration_seconds > sample_seconds * 3 else 0
    cmd = [ffmpeg_exe, "-hide_banner", "-nostats", "-loglevel", "error",
           "-ss", f"{start_at:.1f}", "-i", path, "-t", f"{sample_seconds}",
           *video_args(info), "-an", "-sn", "-f", "null", "-"]
    t0 = time.monotonic()
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=180, creationflags=_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return None
    wall = time.monotonic() - t0
    if r.returncode != 0 or wall <= 0:
        return None
    seconds = min(sample_seconds, max(info.duration_seconds - start_at, 0)) or sample_seconds
    return seconds * (info.fps or 25.0) * _output_pixels(info) / wall


def format_duration(seconds: float) -> str:
    seconds = int(max(seconds, 0))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def _keep_awake(on: bool) -> None:
    """Stop Windows sleeping mid-batch (applies to the calling thread)."""
    if sys.platform != "win32":
        return
    ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
    flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0)
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(flags)
    except (AttributeError, OSError):
        pass


def short_reason(msg: str) -> str:
    """One readable line from an exception message (ffmpeg tails are long)."""
    lines = [l.strip() for l in msg.splitlines() if l.strip()]
    if not lines:
        return "Unknown error"
    first = lines[0]
    detail = next(
        (l for l in reversed(lines[1:])
         if not l.lower().startswith(("conversion failed", "exiting normally"))),
        "",
    )
    return f"{first} {detail}".strip() if detail else first


def output_looks_complete(output: str, source: VideoInfo, ffprobe_exe: str) -> bool:
    try:
        out = probe(output, ffprobe_exe=ffprobe_exe)
    except Exception:
        return False
    if source.duration_seconds <= 0:
        return out.duration_seconds > 0
    return abs(out.duration_seconds - source.duration_seconds) <= max(
        2.0, source.duration_seconds * 0.01)


def _safe(callback: Callable, log_name: str) -> Callable:
    def call(*args) -> None:
        try:
            callback(*args)
        except Exception:
            logging.getLogger(log_name).exception("UI callback failed")
    return call


@dataclass
class BatchResult:
    cancelled: bool
    log_path: str
    summary_path: str
    summary_text: str
    counts: dict = field(default_factory=dict)


class BatchRunner:
    """
    Runs queued items one at a time on a worker thread. Callbacks are invoked
    from the worker thread; the UI must marshal them to its own thread.
    """

    def __init__(
        self,
        items: list[BatchItem],
        show: str,
        year: Optional[str],
        output_dir: str,
        ffmpeg_exe: str,
        ffprobe_exe: str,
        log_dir: str,
        rates: Rates,
        on_item: Callable[[BatchItem], None] = lambda _i: None,
        on_progress: Callable[[BatchItem, float], None] = lambda _i, _f: None,
        on_notice: Callable[[str], None] = lambda _m: None,
        on_rates: Callable[[Rates], None] = lambda _r: None,
        on_finished: Callable[[BatchResult], None] = lambda _r: None,
        stall_timeout: float = 600.0,
        keep_names: bool = False,
    ) -> None:
        self.items = items
        self.show, self.year, self.output_dir = show, year, output_dir
        self.ffmpeg_exe, self.ffprobe_exe = ffmpeg_exe, ffprobe_exe
        self.rates = rates
        # Callbacks are wrapped so a UI error can never fail a conversion.
        name = self._log_name = f"plexconverter.batch.{id(self)}"
        self.on_item, self.on_progress = _safe(on_item, name), _safe(on_progress, name)
        self.on_notice, self.on_rates = _safe(on_notice, name), _safe(on_rates, name)
        self.on_finished = _safe(on_finished, name)
        self.stall_timeout = stall_timeout
        self.keep_names = keep_names

        self.cancel_event = threading.Event()
        self.pause_event = threading.Event()
        self.current: Optional[BatchItem] = None
        self.current_fraction = 0.0
        self._to_convert: set[int] = set()

        stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        os.makedirs(log_dir, exist_ok=True)
        self.log_path = os.path.join(log_dir, f"batch_{stamp}.log")
        self.summary_path = os.path.join(log_dir, f"batch_{stamp}_summary.txt")
        self._thread: Optional[threading.Thread] = None
        self._started = datetime.now()

    # --------------------------------------------------------------- control

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def pause(self) -> None:
        self.pause_event.set()

    def resume(self) -> None:
        self.pause_event.clear()

    def cancel(self) -> None:
        self.cancel_event.set()
        self.pause_event.clear()

    @property
    def paused(self) -> bool:
        return self.pause_event.is_set()

    def join(self, timeout: Optional[float] = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    def remaining_seconds(self) -> float:
        total = 0.0
        for it in self.items:
            if it.info is None:
                continue
            if it is self.current:
                total += estimate_item(it.info, self.rates) * (1 - self.current_fraction)
            elif it.status == QUEUED and id(it) in self._to_convert:
                total += estimate_item(it.info, self.rates)
        return total

    # --------------------------------------------------------------- worker

    def _set(self, item: BatchItem, status: str, reason: str = "") -> None:
        item.status, item.reason = status, reason
        self.on_item(item)

    def _wait_while_paused(self) -> None:
        while self.pause_event.is_set() and not self.cancel_event.is_set():
            time.sleep(0.2)

    def _enough_space(self, item: BatchItem) -> tuple[bool, str]:
        size = item.info.size_bytes if item.info else 0
        need = int(size * (2.0 if item.info and item.info.needs_reencode else 1.1)) + (1 << 30)
        try:
            free = shutil.disk_usage(self.output_dir).free
        except OSError:
            return True, ""
        if free >= need:
            return True, ""
        return False, (f"Low disk space on the output drive ({free / 2**30:.1f} GB free, "
                       f"about {need / 2**30:.1f} GB needed). Batch paused — free some "
                       f"space and press Resume.")

    def _run(self) -> None:
        log = logging.getLogger(self._log_name)
        log.setLevel(logging.INFO)
        log.propagate = False
        handler = logging.FileHandler(self.log_path, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s",
                                               "%Y-%m-%d %H:%M:%S"))
        log.addHandler(handler)
        _keep_awake(True)
        try:
            self._run_queue(log)
        except Exception as exc:  # never leave the UI hanging
            log.exception("Batch runner crashed: %s", exc)
        finally:
            _keep_awake(False)
            result = self._write_summary(log)
            log.removeHandler(handler)
            handler.close()
            self.on_finished(result)

    def _title(self) -> str:
        if self.keep_names:
            return "(keeping original filenames)"
        return f"{self.show} ({self.year})" if self.year else self.show

    def _run_queue(self, log: logging.Logger) -> None:
        log.info("Batch started: %d files, show %s, output %s", len(self.items), self._title(),
                 self.output_dir)
        log.info("ffmpeg: %s", self.ffmpeg_exe)
        os.makedirs(self.output_dir, exist_ok=True)

        plans = plan_items(self.items, self.show, self.year, self.output_dir, self.keep_names)
        self._to_convert = {id(i) for i in self.items
                            if plans[id(i)].action in ("reencode", "remux")}
        for it in self.items:
            plan = plans[id(it)]
            if it.status != QUEUED:
                continue
            if plan.action == "probing":
                plan = Plan("fail", "File was never inspected")
            if plan.action == "fail":
                self._set(it, FAILED, plan.note)
                log.error("FAILED  %s — %s", it.path, plan.note)
            elif plan.action == "skip" and plan.note != "Already converted":
                self._set(it, SKIPPED, plan.note.replace(" — double-click to set", ""))
                log.warning("SKIPPED %s — %s", it.path, it.reason)

        for index, it in enumerate(self.items, 1):
            if it.status != QUEUED:
                continue
            self._wait_while_paused()
            if self.cancel_event.is_set():
                break
            self._process(it, index, log)

        if self.cancel_event.is_set():
            log.warning("Batch cancelled by user.")

    def _process(self, it: BatchItem, index: int, log: logging.Logger) -> None:
        name = os.path.basename(it.path)
        out = it.output_path

        if os.path.exists(out):
            if it.info and output_looks_complete(out, it.info, self.ffprobe_exe):
                self._set(it, SKIPPED, "Already converted")
                log.info("SKIPPED %s — output already exists: %s", it.path, out)
                return
            log.warning("Existing output looks incomplete, replacing it: %s", out)

        while True:
            ok, msg = self._enough_space(it)
            if ok or self.cancel_event.is_set():
                break
            log.warning(msg)
            self.pause_event.set()
            self.on_notice(msg)
            self._wait_while_paused()
        if self.cancel_event.is_set():
            return

        op = "Re-encode" if it.info.needs_reencode else "Remux"
        self.current, self.current_fraction = it, 0.0
        self._set(it, CONVERTING, op)
        log.info("[%d/%d] %s  %s -> %s", index, len(self.items), op.upper(), it.path, out)
        log.info("        codec=%s %dx%d %.1ffps %s, %.0fs", it.info.codec, it.info.width,
                 it.info.height, it.info.fps, it.info.profile or "", it.info.duration_seconds)
        tracks = []
        for s in it.info.streams:
            note = ""
            if s.kind == "subtitle" and subtitle_action(s.codec) != "copy":
                note = {"srt": " -> SRT", "vobsub": " -> VobSub",
                        "unsupported": " (MKV can't store this)"}[subtitle_action(s.codec)]
            tracks.append(s.label + note)
        log.info("        tracks: %s", "; ".join(tracks) or "(unknown)")
        for s in dropped_streams(it.info):
            log.info("        not carried over: %s", s.label)
        subs = out + SUBS_SUFFIX if any(subtitle_action(s.codec) == "vobsub"
                                        for s in it.info.streams if s.kind == "subtitle") else None
        if subs:
            log.info("        cmd: %s", subprocess.list2cmdline(
                build_subs_cmd(it.path, subs, it.info, self.ffmpeg_exe)))
        log.info("        cmd: %s", subprocess.list2cmdline(
            build_cmd(it.path, out + PART_SUFFIX, it.info, self.ffmpeg_exe, subs)))

        paused_total = [0.0]
        t0 = time.monotonic()

        def _progress(f: float) -> None:
            self.current_fraction = f
            self.on_progress(it, f)

        # Track paused time so throughput isn't skewed by pauses.
        stop = threading.Event()

        def _pause_clock() -> None:
            while not stop.wait(0.25):
                if self.pause_event.is_set():
                    paused_total[0] += 0.25

        clock = threading.Thread(target=_pause_clock, daemon=True)
        clock.start()
        try:
            convert(it.path, out, it.info, ffmpeg_exe=self.ffmpeg_exe,
                    progress_callback=_progress, cancel_event=self.cancel_event,
                    pause_event=self.pause_event, stall_timeout=self.stall_timeout,
                    ffprobe_exe=self.ffprobe_exe)
        except ConversionCancelled:
            it.status = QUEUED  # not finished; a re-run will pick it up
            it.reason = ""
            self.on_item(it)
            log.warning("CANCELLED %s (partial output removed)", it.path)
            return
        except Exception as exc:
            detail = str(exc)
            self._set(it, FAILED, short_reason(detail))
            log.error("FAILED  %s\n%s", it.path, detail.rstrip())
            return
        finally:
            stop.set()
            clock.join(timeout=1)
            self.current = None

        it.elapsed = max(time.monotonic() - t0 - paused_total[0], 0.1)
        self._set(it, DONE, f"{op} in {format_duration(it.elapsed)}")
        log.info("DONE    %s in %s", name, format_duration(it.elapsed))
        self._learn(it)

    def _learn(self, it: BatchItem) -> None:
        """Refine the estimate from real conversions."""
        info = it.info
        if it.elapsed < 5:
            return
        if info.needs_reencode:
            measured = info.duration_seconds * (info.fps or 25.0) * _output_pixels(info) / it.elapsed
            self.rates.encode_px_per_sec = (
                measured if self.rates.source != "previous conversions"
                else 0.5 * self.rates.encode_px_per_sec + 0.5 * measured)
        elif info.size_bytes:
            measured = info.size_bytes / it.elapsed
            self.rates.remux_bytes_per_sec = 0.5 * self.rates.remux_bytes_per_sec + 0.5 * measured
        self.rates.source = "previous conversions"
        self.on_rates(self.rates)

    # --------------------------------------------------------------- summary

    def _write_summary(self, log: logging.Logger) -> BatchResult:
        finished = datetime.now()
        by = {s: [i for i in self.items if i.status == s] for s in (DONE, SKIPPED, FAILED, QUEUED)}
        cancelled = self.cancel_event.is_set()
        title = self._title()

        lines = [
            "Plex Converter — batch summary",
            "=" * 60,
            f"Show:      {title}",
            f"Output:    {self.output_dir}",
            f"Started:   {self._started:%Y-%m-%d %H:%M:%S}",
            f"Finished:  {finished:%Y-%m-%d %H:%M:%S}  "
            f"({format_duration((finished - self._started).total_seconds())})",
            f"Result:    {'CANCELLED' if cancelled else 'COMPLETED'}",
            "",
            f"Converted: {len(by[DONE])}",
            f"Skipped:   {len(by[SKIPPED])}",
            f"Failed:    {len(by[FAILED])}",
        ]
        if by[QUEUED]:
            lines.append(f"Not run:   {len(by[QUEUED])} (cancelled before they started)")
        lines.append("")

        def section(header: str, items: list[BatchItem], fmt: Callable[[BatchItem], str]) -> None:
            if not items:
                return
            lines.append(f"{header} ({len(items)})")
            lines.append("-" * 60)
            for i in items:
                lines.append(f"  {i.display_name}")
                lines.append(f"      {fmt(i)}")
            lines.append("")

        section("FAILED", by[FAILED], lambda i: i.reason)
        section("SKIPPED", by[SKIPPED], lambda i: i.reason +
                (f": {i.output_path}" if i.reason == "Already converted" else ""))
        section("CONVERTED", by[DONE], lambda i: f"{i.reason} -> {i.output_path}")
        section("NOT RUN", by[QUEUED], lambda i: "Cancelled before it started")
        lines.append(f"Full log: {self.log_path}")
        text = "\n".join(lines)

        try:
            Path(self.summary_path).write_text(text + "\n", encoding="utf-8")
        except OSError as exc:
            log.error("Could not write summary: %s", exc)
        log.info("Batch finished: %d converted, %d skipped, %d failed, %d not run",
                 len(by[DONE]), len(by[SKIPPED]), len(by[FAILED]), len(by[QUEUED]))
        return BatchResult(
            cancelled=cancelled, log_path=self.log_path, summary_path=self.summary_path,
            summary_text=text,
            counts={"converted": len(by[DONE]), "skipped": len(by[SKIPPED]),
                    "failed": len(by[FAILED]), "not_run": len(by[QUEUED])},
        )

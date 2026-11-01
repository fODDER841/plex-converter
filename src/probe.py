import json
import os
import subprocess
import sys
from dataclasses import dataclass, field

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

# Codecs Plex handles natively — remux only, no re-encode needed.
_COMPATIBLE = frozenset({"h264", "hevc", "h265"})


@dataclass
class StreamInfo:
    index: int              # absolute stream index in the source file
    kind: str               # "video" | "audio" | "subtitle" | "attachment" | "data"
    codec: str
    language: str = ""
    attached_pic: bool = False  # cover art stored as a video stream

    @property
    def label(self) -> str:
        lang = f" [{self.language}]" if self.language else ""
        return f"#{self.index} {self.kind} {self.codec}{lang}"


@dataclass
class VideoInfo:
    codec: str
    profile: str
    width: int
    height: int
    duration_seconds: float
    needs_reencode: bool
    fps: float = 0.0
    size_bytes: int = 0
    streams: list[StreamInfo] = field(default_factory=list)

    def count(self, kind: str) -> int:
        return sum(1 for s in self.streams if s.kind == kind and not s.attached_pic)

    @property
    def operation_label(self) -> str:
        return "Re-encode → H.264" if self.needs_reencode else "Remux (stream copy)"


def probe(path: str, ffprobe_exe: str = "ffprobe") -> VideoInfo:
    try:
        result = subprocess.run(
            [
                ffprobe_exe, "-v", "quiet",
                "-print_format", "json",
                "-show_streams", "-show_format",
                path,
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
            creationflags=_NO_WINDOW,
        )
    except FileNotFoundError:
        raise RuntimeError(
            f"ffprobe not found at: {ffprobe_exe}\n"
            "Use the FFmpeg Settings button to locate the binaries."
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            "ffprobe timed out after 30 seconds. "
            "The file may be on slow storage or corrupt."
        )
    except subprocess.CalledProcessError as e:
        detail = (e.stderr or "").strip()
        raise RuntimeError(
            f"ffprobe failed:\n{detail}" if detail
            else "Not a readable video file (corrupt, incomplete, or not a video)."
        )

    data = json.loads(result.stdout)

    streams = [
        StreamInfo(
            index=int(s.get("index", i)),
            kind=s.get("codec_type", "data"),
            codec=(s.get("codec_name") or "unknown").lower(),
            language=(s.get("tags") or {}).get("language", ""),
            attached_pic=bool((s.get("disposition") or {}).get("attached_pic")),
        )
        for i, s in enumerate(data.get("streams", []))
    ]
    video = next(
        (s for s in data.get("streams", [])
         if s.get("codec_type") == "video"
         and not (s.get("disposition") or {}).get("attached_pic")),
        None,
    )
    if video is None:
        raise RuntimeError("No video stream found in this file.")

    codec = video.get("codec_name", "unknown").lower()
    profile = video.get("profile", "")
    width = int(video.get("width", 0))
    height = int(video.get("height", 0))
    duration = float(data.get("format", {}).get("duration") or 0)
    fps = _parse_rate(video.get("avg_frame_rate")) or _parse_rate(video.get("r_frame_rate"))
    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0

    # 10-bit H.264 must be re-encoded — most hardware decoders reject it.
    is_10bit_h264 = codec == "h264" and "10" in profile
    needs_reencode = (codec not in _COMPATIBLE) or is_10bit_h264

    return VideoInfo(
        codec=codec,
        profile=profile,
        width=width,
        height=height,
        duration_seconds=duration,
        needs_reencode=needs_reencode,
        fps=fps,
        size_bytes=size,
        streams=streams,
    )


def _parse_rate(rate: str | None) -> float:
    """ffprobe frame rates look like "24000/1001"; "0/0" means unknown."""
    try:
        num, _, den = (rate or "").partition("/")
        value = float(num) / float(den or 1)
    except (ValueError, ZeroDivisionError):
        return 0.0
    return value if 0 < value < 1000 else 0.0

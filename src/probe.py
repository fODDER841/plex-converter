import json
import subprocess
from dataclasses import dataclass

# Codecs Plex handles natively — remux only, no re-encode needed.
_COMPATIBLE = frozenset({"h264", "hevc", "h265"})


@dataclass
class VideoInfo:
    codec: str
    profile: str
    width: int
    height: int
    duration_seconds: float
    needs_reencode: bool

    @property
    def operation_label(self) -> str:
        return "Re-encode → H.264" if self.needs_reencode else "Remux (stream copy)"


def probe(path: str) -> VideoInfo:
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "quiet",
                "-print_format", "json",
                "-show_streams", "-show_format",
                path,
            ],
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError:
        raise RuntimeError(
            "ffprobe not found. Install ffmpeg and make sure it is on your PATH."
        )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"ffprobe failed:\n{e.stderr}")

    data = json.loads(result.stdout)

    video = next(
        (s for s in data.get("streams", []) if s.get("codec_type") == "video"),
        None,
    )
    if video is None:
        raise RuntimeError("No video stream found in this file.")

    codec = video.get("codec_name", "unknown").lower()
    profile = video.get("profile", "")
    width = int(video.get("width", 0))
    height = int(video.get("height", 0))
    duration = float(data.get("format", {}).get("duration") or 0)

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
    )

"""Generate small test media with ffmpeg, including multi-track files."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

SRT = "1\n00:00:00,500 --> 00:00:02,000\nHello\n\n2\n00:00:02,500 --> 00:00:03,500\nWorld\n"


def _lavfi_av(seconds: int, size: str = "320x240", tones=(440,)) -> list[str]:
    args = ["-f", "lavfi", "-i", f"testsrc=duration={seconds}:size={size}:rate=25"]
    for f in tones:
        args += ["-f", "lavfi", "-i", f"sine=duration={seconds}:frequency={f}"]
    return args


def run(ffmpeg: str, args: list[str]) -> None:
    subprocess.run([ffmpeg, "-y", "-v", "error", *args], check=True, creationflags=_NO_WINDOW)


# ---------------------------------------------------------------------------
# Minimal VobSub (.idx + .sub) writer. ffmpeg can't turn text into bitmap
# subtitles, so this is the only way to get a bitmap source, from which
# ffmpeg can then make DivX "xsub" subtitles like real AVI releases have.
# ---------------------------------------------------------------------------

def _pts_bytes(pts: int) -> bytes:
    return bytes([0x21 | ((pts >> 29) & 0x0E), (pts >> 22) & 0xFF,
                  0x01 | ((pts >> 14) & 0xFE), (pts >> 7) & 0xFF, 0x01 | ((pts << 1) & 0xFE)])


def _spu(width: int = 64, height: int = 16, x: int = 20, y: int = 20, ticks: int = 150) -> bytes:
    line = b"\x00\x01"  # RLE: fill the rest of the line with colour 1
    top, bottom = line * ((height + 1) // 2), line * (height // 2)
    pixels = top + bottom
    ctrl1 = 4 + len(pixels)
    x2, y2 = x + width - 1, y + height - 1
    coords = bytes([x >> 4, ((x & 0xF) << 4) | (x2 >> 8), x2 & 0xFF,
                    y >> 4, ((y & 0xF) << 4) | (y2 >> 8), y2 & 0xFF])
    body = (b"\x01" + b"\x03\x32\x10" + b"\x04\xFF\xF0" + b"\x05" + coords
            + b"\x06" + (4).to_bytes(2, "big") + (4 + len(top)).to_bytes(2, "big") + b"\xFF")
    ctrl2 = ctrl1 + 4 + len(body)
    data = (pixels + b"\x00\x00" + ctrl2.to_bytes(2, "big") + body
            + ticks.to_bytes(2, "big") + ctrl2.to_bytes(2, "big") + b"\x02\xFF")
    return (4 + len(data)).to_bytes(2, "big") + ctrl1.to_bytes(2, "big") + data


def _pack(pts: int, spu: bytes) -> bytes:
    header = b"\x00\x00\x01\xBA" + bytes.fromhex("4400040004010189C3F8")
    pes_hdr = b"\x81\x80\x05" + _pts_bytes(pts)
    payload = b"\x20" + spu
    pes = b"\x00\x00\x01\xBD" + (len(pes_hdr) + len(payload)).to_bytes(2, "big") + pes_hdr + payload
    pad = 2048 - len(header) - len(pes) - 6
    return header + pes + b"\x00\x00\x01\xBE" + pad.to_bytes(2, "big") + b"\xFF" * pad


def make_vobsub(base: Path, times_ms=(500, 2500)) -> Path:
    sub, entries = b"", []
    for t in times_ms:
        entries.append(f"timestamp: 00:00:{t // 1000:02d}:{t % 1000:03d}, filepos: {len(sub):09x}")
        sub += _pack(t * 90, _spu())
    base.with_suffix(".sub").write_bytes(sub)
    palette = ", ".join(["000000", "ffffff", "808080", "c0c0c0"] + ["000000"] * 12)
    base.with_suffix(".idx").write_text("\n".join([
        "# VobSub index file, v7 (do not modify this line!)", "size: 320x240",
        f"palette: {palette}", "id: en, index: 0", *entries, ""]))
    return base.with_suffix(".idx")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def make_avi(ffmpeg: str, path: Path, codec: str = "mpeg4", seconds: int = 4,
             size: str = "320x240") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    vcodec = ["-c:v", "mpeg4", "-vtag", "XVID", "-q:v", "5"] if codec == "mpeg4" else \
             ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    run(ffmpeg, [*_lavfi_av(seconds, size), *vcodec, "-c:a", "libmp3lame", "-shortest",
                 str(path)])


def make_dual_xsub_avi(ffmpeg: str, path: Path, seconds: int = 4) -> None:
    """Xvid AVI with two MP3 audio tracks and two DivX (xsub) subtitle tracks."""
    path.parent.mkdir(parents=True, exist_ok=True)
    idx = make_vobsub(path.with_name(path.stem + "_subs"))
    run(ffmpeg, [*_lavfi_av(seconds, tones=(440, 880)), "-i", str(idx), "-i", str(idx),
                 "-map", "0", "-map", "1", "-map", "2", "-map", "3", "-map", "4",
                 "-c:v", "mpeg4", "-vtag", "XVID", "-c:a", "libmp3lame", "-c:s", "xsub",
                 "-shortest", str(path)])


def make_dual_mkv(ffmpeg: str, path: Path, font: Path | None, seconds: int = 4) -> None:
    """H.264 MKV: eng+jpn audio, eng SRT + jpn ASS subtitles, optional font attachment."""
    path.parent.mkdir(parents=True, exist_ok=True)
    srt = path.with_suffix(".srt")
    srt.write_text(SRT)
    args = [*_lavfi_av(seconds, tones=(440, 880)), "-i", str(srt), "-i", str(srt)]
    if font:
        args += ["-attach", str(font), "-metadata:s:t", "mimetype=application/x-truetype-font"]
    args += ["-map", "0", "-map", "1", "-map", "2", "-map", "3", "-map", "4",
             "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-c:s:0", "srt", "-c:s:1", "ass",
             "-metadata:s:a:0", "language=eng", "-metadata:s:a:1", "language=jpn",
             "-metadata:s:s:0", "language=eng", "-metadata:s:s:1", "language=jpn",
             "-shortest", str(path)]
    run(ffmpeg, args)


def make_mov_text_mp4(ffmpeg: str, path: Path, seconds: int = 4) -> None:
    """H.264 MP4 with two AAC tracks and two mov_text subtitle tracks."""
    path.parent.mkdir(parents=True, exist_ok=True)
    srt = path.with_suffix(".srt")
    srt.write_text(SRT)
    run(ffmpeg, [*_lavfi_av(seconds, tones=(440, 880)), "-i", str(srt), "-i", str(srt),
                 "-map", "0", "-map", "1", "-map", "2", "-map", "3", "-map", "4",
                 "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                 "-c:a", "aac", "-c:s", "mov_text", "-shortest", str(path)])

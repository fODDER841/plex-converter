# Plex Converter

A Windows desktop app that converts video files to Plex-compatible MKV format.
Drag a file onto the window, confirm the title and year, and click **Convert**.

---

## Requirements

| Dependency | Where to get it |
|---|---|
| Python 3.10+ | https://www.python.org/downloads/ |
| ffmpeg + ffprobe on PATH | https://ffmpeg.org/download.html |

> **ffmpeg doesn't need to be installed beforehand.** If the app can't find it at
> startup, a setup dialog offers to install it automatically (via
> `winget install --id Gyan.FFmpeg`), or to point at an existing folder. On older
> Windows without winget, it links to App Installer and a direct ffmpeg .zip instead.
> Conversion is disabled until setup succeeds; no restart is needed afterwards.

---

## Setup

```bash
pip install -r requirements.txt
python app.py
```

That's it. No build step needed to run from source.

---

## How to use it

1. **Drop** a video file onto the window, or click the drop zone to browse.
2. The app probes the file with `ffprobe` and shows detected codec and resolution.
3. **Check/correct** the title, year, and whether it's a movie or TV show. For TV shows, also set season and episode numbers.
4. **Check/set** the output folder. The choice is remembered between runs.
5. Click **Convert**.

A progress bar shows percentage complete while ffmpeg runs. You can cancel mid-conversion with the **Cancel** button. The partial output file is deleted automatically when cancelled.

---

## Conversion logic

| Input video codec | Action |
|---|---|
| H.264 (8-bit) | Remux to MKV — streams copied as-is, no quality loss |
| HEVC / H.265 | Remux to MKV — streams copied as-is, no quality loss |
| Everything else (MPEG-2, VC-1, DivX, Xvid, etc.) | Re-encode → H.264 CRF 18, preset medium |
| H.264 10-bit | Re-encode → H.264 8-bit CRF 18 (most hardware decoders reject 10-bit H.264) |

**Resolution:** When re-encoding, resolution is capped at 1080p. Sources smaller than 1080p are kept at their original resolution (no upscaling).

**Audio:** All audio streams are always copied untouched.

**Subtitles:** All subtitle streams (SRT, PGS, VobSub, etc.) are copied as-is.

---

## Output naming

The app follows Plex's naming convention so the library scanner picks up metadata automatically.

**Movie:**
```
<Output Dir>/
  The Dark Knight (2008)/
    The Dark Knight (2008).mkv
```

**TV Show:**
```
<Output Dir>/
  Breaking Bad (2008)/
    Season 01/
      Breaking Bad - S01E01.mkv
```

---

## Filename parsing

The app tries to extract a clean title and year from messy filenames:

| Input filename | Parsed as |
|---|---|
| `The.Dark.Knight.2008.BluRay.1080p.x264-GROUP.mkv` | Movie · The Dark Knight · 2008 |
| `Breaking.Bad.S01E01.720p.HDTV.mkv` | TV · Breaking Bad · S01E01 |
| `movie_title_no_year.avi` | Movie · Movie Title No Year · (blank — fill in manually) |

You can always edit all fields before clicking Convert.

---

## Settings

Output folder preference is saved to:
```
%APPDATA%\PlexConverter\settings.json
```

---

## Building a standalone .exe

```bash
pip install pyinstaller
pyinstaller --onefile --windowed app.py
```

The executable will appear at `dist/app.exe`. You can distribute this single file to a machine that does not have Python installed; ffmpeg is set up on first run (see Requirements).

---

## Tests

The first-run setup flow is tested against a sandbox with no ffmpeg (fake `winget`,
isolated PATH/LOCALAPPDATA/settings):

```bash
python -m unittest tests.test_ffmpeg_setup -v
```

Set `SETUP_SCREENSHOTS=<dir>` to save a PNG of each dialog state, and
`RUN_REAL_WINGET=1` to also invoke the real winget CLI.

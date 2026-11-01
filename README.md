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

## Batch conversion (whole TV series)

Drop a **folder** or **several files** onto the window, or click **Convert a whole
folder (TV series)…**. Folders are searched recursively for video files; `sample`
clips are ignored.

1. The app inspects every file, then shows the queue. For each file you see the
   parsed episode (`s01e05`), the video codec, and whether it will be **remuxed** or
   **re-encoded**.
2. Set the **show title** and **year** once. They apply to every file. Season and
   episode numbers come from each filename (`S01E05`, `1x05`, `Season 1/05 - Title`,
   `Show 105`, multi-episode `S01E01E02`, and so on).
3. Files that need a decision are shown in orange: no episode number found, two
   files mapping to the same episode, an unreadable file, or a guessed number from an
   ambiguous name like `Show 105`. Tick **Show only files that need attention** to
   see just those. Double-click a row to set its season/episode, or remove it with
   **Remove selected** (or the Delete key).
4. Before you start, the app shows an **estimated time** based on how many files
   need re-encoding versus remuxing. The first time, it measures your computer's
   encoding speed with a short test encode. After that it uses timings from your
   real conversions.
5. Click **Start batch**. Files are converted **one at a time**, since ffmpeg already
   uses every core. You get overall progress, progress on the current file, and a
   remaining-time estimate. **Pause** suspends ffmpeg mid-file; **Cancel** stops the
   batch and discards only the file in progress.

**Keep original filenames.** For collections that aren't conventional TV, such as
standalone shorts named `1993 - ShowName - Title.avi`, tick **Keep original
filenames**. Nothing is parsed or renamed and no show or season folders are made.
Each file keeps its name exactly, only the extension becomes `.mkv`, and every
output goes straight into the output folder:

```
<Output Dir>/
  1993 - ShowName - Title.mkv
  1994 - ShowName - Second Short.mkv
```

If two files in different subfolders have the same name, both are flagged instead
of one overwriting the other.

**Resilience**

- A failed file is logged, marked **failed**, and the queue moves on.
- Output is written to a temporary `.part` file and renamed only when ffmpeg
  succeeds, so an existing output file is always a finished one. Re-running the same
  folder after an interruption **skips files that are already converted**. An
  existing output whose length doesn't match the source is treated as incomplete
  and replaced.
- If ffmpeg makes no progress for 10 minutes, it is stopped and that file is marked failed.
- If the output drive is low on space, the batch pauses until you free space and
  press **Resume**.
- The computer is kept from going to sleep while a batch runs.

**Log and summary.** Each batch writes two files to `%APPDATA%\PlexConverter\logs\`:

- `batch_<date>_<time>.log` — every step, including the ffmpeg command and error output for failures.
- `batch_<date>_<time>_summary.txt` — counts of converted, skipped, and failed files, with the reason for each skip or failure.

The summary is also shown in the app when the batch ends. **Open log folder** takes you there.

---

## Conversion logic

| Input video codec | Action |
|---|---|
| H.264 (8-bit) | Remux to MKV — streams copied as-is, no quality loss |
| HEVC / H.265 | Remux to MKV — streams copied as-is, no quality loss |
| Everything else (MPEG-2, VC-1, DivX, Xvid, etc.) | Re-encode → H.264 CRF 18, preset medium |
| H.264 10-bit | Re-encode → H.264 8-bit CRF 18 (most hardware decoders reject 10-bit H.264) |

**Resolution:** When re-encoding, resolution is capped at 1080p. Sources smaller than 1080p are kept at their original resolution (no upscaling).

**Tracks:** Every video, audio and subtitle track is kept, in its original order,
with its language tag. Dual-audio files keep both audio tracks. Chapters and MKV
attachments, such as fonts used by styled subtitles, are kept too. Cover-art images
and data tracks are not carried over; the batch log lists them.

**Audio:** All audio tracks are copied untouched.

**Subtitles:** MKV can't store every subtitle format, so each track is handled by type:

| Subtitle format | What happens |
|---|---|
| SRT, ASS/SSA, WebVTT, PGS (Blu-ray), VobSub (DVD), DVB | Copied as-is |
| MP4 text (`mov_text`) and other text formats | Converted to SRT, so the text and timing are kept |
| DivX subtitles (`xsub`, common in AVI) | Converted to VobSub (image-based, like the original), with timing kept |
| Teletext (`dvb_teletext`) | MKV can't store it. The file is **not converted** and is marked failed with that reason, so no track is silently lost |

**Safety check:** after every conversion, the app reads the new file back and
compares its audio and subtitle track counts with the source. If any track is
missing, the file is marked failed and not saved.

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
      Breaking Bad (2008) - s01e01.mkv
      Breaking Bad (2008) - s01e02-e03.mkv   (one file containing two episodes)
```

Characters Windows doesn't allow in file names are removed from titles. `:` becomes ` -`, so
`Star Trek: The Next Generation` becomes `Star Trek - The Next Generation`.

**Episodes converted by older versions** were named `Breaking Bad - S01E01.mkv`, with no
year in the file name. To have batch conversion recognise them as already done,
rename them to the current format with the included script. It does a dry run by default:

```bash
python scripts\rename_old_tv_names.py "D:\TV"           # show what would change
python scripts\rename_old_tv_names.py "D:\TV" --apply   # rename
```

Point it at the whole library or a single show folder. It only touches files that
exactly match the old pattern inside a `Season NN` folder whose show folder matches
the title. Anything else is left alone and reported, including cases where a file
with the new name already exists. With `--apply`, a CSV listing every rename is
saved to `%APPDATA%\PlexConverter\logs\` so the renames can be undone.

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

Batch conversion is tested end to end against small files generated with ffmpeg
(`tests/media.py`). These include an Xvid AVI with two audio tracks and two DivX
subtitle tracks, an MKV with language-tagged audio, SRT and ASS subtitles and an
embedded font, an MP4 with text subtitles, and a corrupt file. The tests cover:

- filename parsing
- keeping every track, with subtitle timing checked
- the missing-track safety check
- keep-original-filenames mode
- the legacy rename script
- failure isolation and skip-on-rerun
- replacing incomplete outputs
- cancel, pause, and the stall watchdog
- duplicate episodes

Run the whole suite with:

```bash
python -m unittest tests.test_ffmpeg_setup tests.test_batch -v
```

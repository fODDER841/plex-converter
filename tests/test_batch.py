"""
Batch conversion tests: filename parsing, planning, and the runner end to end
against small AVI files generated with ffmpeg (skipped if ffmpeg is missing).

Run:  python -m unittest tests.test_batch -v
"""
from __future__ import annotations

import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "scripts"))

import media  # noqa: E402
import rename_old_tv_names as legacy  # noqa: E402
import src.ffmpeg_finder as finder  # noqa: E402
from src import batch, converter  # noqa: E402
from src.converter import PART_SUFFIX, SUBS_SUFFIX, convert  # noqa: E402
from src.naming import flat_output_path, parse_episode, plex_tv_path  # noqa: E402
from src.probe import StreamInfo, VideoInfo, probe  # noqa: E402

FFMPEG, FFPROBE, _ = finder.resolve(None)
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
FONT = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "arial.ttf"


def make_avi(path: Path, codec: str = "mpeg4", seconds: int = 4, size: str = "320x240") -> None:
    media.make_avi(FFMPEG, path, codec, seconds, size)


def tracks(path) -> list[tuple[str, str, str]]:
    return [(s.kind, s.codec, s.language) for s in probe(str(path), FFPROBE).streams]


def subtitle_times(path, stream: int) -> list[tuple[int, int]]:
    """(display start ms, duration ms) for each subtitle event in a stream."""
    r = subprocess.run(
        [FFPROBE, "-v", "error", "-select_streams", f"s:{stream}", "-show_frames",
         "-show_entries", "subtitle=pts,start_display_time,end_display_time",
         "-of", "compact=nk=1", str(path)],
        capture_output=True, text=True, check=True, creationflags=_NO_WINDOW)
    out = []
    for line in r.stdout.split():
        _, pts, start, end = line.split("|")
        out.append((int(pts) // 1000 + int(start), int(end) - int(start)))
    return out


class TestParsing(unittest.TestCase):
    ROOT = r"D:\TV\The Wire (2002)"

    def check(self, rel: str, season, episode, end=None, uncertain=False, title="The Wire"):
        g = parse_episode(os.path.join(self.ROOT, rel), self.ROOT)
        self.assertEqual((g.title, g.season, g.episode, g.episode_end, g.uncertain),
                         (title, season, episode, end, uncertain), rel)

    def test_shapes(self) -> None:
        self.check(r"Season 1\The.Wire.S01E05.DVDRip.XviD-GRP.avi", 1, 5)
        self.check(r"the.wire.s01e05e06.avi", 1, 5, end=6)
        self.check(r"The Wire - 1x05 - The Pager.avi", 1, 5)
        self.check(r"Season 2\05 - Undertow.avi", 2, 5)
        self.check(r"Season 2\205 - Undertow.avi", 2, 5)
        self.check(r"S03\Episode 7.avi", 3, 7)
        self.check(r"Season 1\S01E05.avi", 1, 5)
        self.check(r"the wire 105.avi", 1, 5, uncertain=True)
        self.check(r"Season 2\the wire 205.avi", 2, 5)
        self.check(r"Extras\Making of.avi", None, None, title="Extras")

    def test_year_from_folder_and_not_resolution(self) -> None:
        g = parse_episode(r"D:\dl\Doctor.Who.2005.S02E01.1920x1080.avi", r"D:\dl")
        self.assertEqual((g.title, g.year, g.season, g.episode), ("Doctor Who", "2005", 2, 1))
        g = parse_episode(r"D:\dl\Show.S01E01-720p.avi", r"D:\dl")
        self.assertEqual((g.episode, g.episode_end), (1, None))

    def test_output_path(self) -> None:
        self.assertEqual(
            plex_tv_path("Star Trek: The Next Generation", "1987", 3, 15, r"D:\TV"),
            r"D:\TV\Star Trek - The Next Generation (1987)\Season 03"
            r"\Star Trek - The Next Generation (1987) - s03e15.mkv")
        self.assertTrue(plex_tv_path("Show", "2001", 1, 1, r"D:\TV", 2).endswith(
            r"Show (2001) - s01e01-e02.mkv"))


@unittest.skipUnless(FFMPEG and FFPROBE, "ffmpeg not available")
class TestRunner(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fixtures = Path(tempfile.mkdtemp(prefix="plexbatch_fx_"))
        show = cls.fixtures / "The Show (2004)"
        make_avi(show / "Season 1" / "The.Show.S01E01.XviD.avi")
        make_avi(show / "Season 1" / "The.Show.S01E02.XviD.avi")
        make_avi(show / "Season 2" / "the show 2x01.avi", codec="h264")
        (show / "Season 2").mkdir(parents=True, exist_ok=True)
        (show / "Season 2" / "The.Show.S02E02.avi").write_bytes(b"RIFF\x00\x00garbage" * 100)
        make_avi(show / "Season 1" / "Sample" / "The.Show.S01E01.sample.avi")
        (show / "Season 1" / "notes.txt").write_text("not a video")
        cls.show_dir = show

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.fixtures, ignore_errors=True)

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="plexbatch_"))
        self.out = self.tmp / "out"
        self.logs = self.tmp / "logs"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def load(self, paths=None) -> list[batch.BatchItem]:
        items = [batch.BatchItem.from_path(p, r)
                 for p, r in batch.collect_videos(paths or [str(self.show_dir)])]
        for it in items:
            try:
                it.info = probe(it.path, FFPROBE)
            except Exception as exc:
                it.probe_error = batch.short_reason(str(exc))
        return items

    def run_batch(self, items, *, before_start=None, **kw) -> batch.BatchResult:
        done = threading.Event()
        result: list[batch.BatchResult] = []
        runner = batch.BatchRunner(
            items, "The Show", "2004", str(self.out), FFMPEG, FFPROBE, str(self.logs),
            batch.default_rates(),
            on_finished=lambda r: (result.append(r), done.set()), **kw)
        runner.start()
        if before_start:
            before_start(runner)
        self.assertTrue(done.wait(120), "batch did not finish")
        runner.join(5)
        return result[0]

    def out_file(self, s: int, e: int) -> Path:
        return Path(plex_tv_path("The Show", "2004", s, e, str(self.out)))

    def no_part_files(self) -> None:
        self.assertEqual(list(self.out.rglob(f"*{PART_SUFFIX}")), [])

    def test_collect_recurses_and_ignores_samples_and_non_video(self) -> None:
        names = [Path(p).name for p, _ in batch.collect_videos([str(self.show_dir)])]
        self.assertEqual(names, ["The.Show.S01E01.XviD.avi", "The.Show.S01E02.XviD.avi",
                                 "the show 2x01.avi", "The.Show.S02E02.avi"])

    def test_failure_does_not_stop_queue_then_rerun_skips_done(self) -> None:
        items = self.load()
        res = self.run_batch(items)
        status = {Path(i.path).name: i.status for i in items}
        self.assertEqual(status, {
            "The.Show.S01E01.XviD.avi": "done", "The.Show.S01E02.XviD.avi": "done",
            "the show 2x01.avi": "done", "The.Show.S02E02.avi": "failed"})
        self.assertEqual(res.counts, {"converted": 3, "skipped": 0, "failed": 1, "not_run": 0})
        for s, e in [(1, 1), (1, 2), (2, 1)]:
            self.assertTrue(self.out_file(s, e).is_file())
        self.assertEqual(probe(str(self.out_file(1, 1)), FFPROBE).codec, "h264")
        self.no_part_files()
        summary = Path(res.summary_path).read_text(encoding="utf-8")
        self.assertIn("Converted: 3", summary)
        self.assertIn("Failed:    1", summary)
        self.assertIn("The.Show.S02E02.avi", summary)
        log = Path(res.log_path).read_text(encoding="utf-8")
        self.assertIn("FAILED", log)
        self.assertIn("REMUX", log)       # the H.264 AVI
        self.assertIn("RE-ENCODE", log)   # the Xvid AVIs

        # Re-run the same folder: finished work is skipped, not redone.
        mtime = self.out_file(1, 1).stat().st_mtime
        items = self.load()
        res = self.run_batch(items)
        self.assertEqual(res.counts, {"converted": 0, "skipped": 3, "failed": 1, "not_run": 0})
        self.assertEqual(self.out_file(1, 1).stat().st_mtime, mtime)
        self.assertIn("Already converted", Path(res.summary_path).read_text(encoding="utf-8"))

    def test_incomplete_existing_output_is_replaced(self) -> None:
        target = self.out_file(1, 1)
        target.parent.mkdir(parents=True)
        target.write_bytes(b"truncated")
        items = [i for i in self.load() if "S01E01" in i.path]
        res = self.run_batch(items)
        self.assertEqual(res.counts["converted"], 1)
        self.assertGreater(probe(str(target), FFPROBE).duration_seconds, 3)

    def test_unparsed_and_duplicates_are_skipped_with_reasons(self) -> None:
        items = self.load()[:3]
        items[1].season, items[1].episode = 1, 1     # duplicate of item 0
        items[2].season = items[2].episode = None    # unparsed
        res = self.run_batch(items)
        self.assertEqual([i.status for i in items], ["skipped"] * 3)
        self.assertTrue(items[0].reason.startswith("Same episode as"))
        self.assertIn("season/episode", items[2].reason)
        self.assertEqual(res.counts["converted"], 0)

    def test_cancel_mid_file_removes_partial_and_requeues(self) -> None:
        big = self.tmp / "src" / "Big.Show.S01E01.avi"
        make_avi(big, seconds=40, size="1280x720")
        items = self.load([str(big)])

        def _cancel_when_running(runner):
            deadline = time.time() + 60
            while runner.current_fraction <= 0.05 and time.time() < deadline:
                time.sleep(0.05)
            runner.cancel()

        res = self.run_batch(items, before_start=_cancel_when_running)
        self.assertTrue(res.cancelled)
        self.assertEqual(items[0].status, "queued")
        self.assertEqual(res.counts["not_run"], 1)
        self.assertFalse(self.out_file(1, 1).exists())
        self.no_part_files()
        self.assertIn("CANCELLED", Path(res.summary_path).read_text(encoding="utf-8"))

    def test_pause_suspends_ffmpeg_and_resume_finishes(self) -> None:
        big = self.tmp / "src" / "Big.Show.S01E01.avi"
        make_avi(big, seconds=40, size="1280x720")
        items = self.load([str(big)])
        observed = {}

        def _pause_then_resume(runner):
            deadline = time.time() + 60
            while runner.current_fraction <= 0.05 and time.time() < deadline:
                time.sleep(0.05)
            runner.pause()
            time.sleep(1.0)  # let the suspend take effect
            before = runner.current_fraction
            time.sleep(2.0)
            observed["delta"] = runner.current_fraction - before
            runner.resume()

        res = self.run_batch(items, before_start=_pause_then_resume)
        self.assertEqual(observed["delta"], 0.0)
        self.assertEqual(res.counts["converted"], 1)

    def test_ui_callback_errors_do_not_fail_conversions(self) -> None:
        items = [i for i in self.load() if "S01E01" in i.path]

        def _boom(*_a):
            raise RuntimeError("main thread is not in main loop")

        res = self.run_batch(items, on_progress=_boom, on_item=_boom)
        self.assertEqual(items[0].status, "done")
        self.assertEqual(res.counts["converted"], 1)
        self.assertIn("UI callback failed", Path(res.log_path).read_text(encoding="utf-8"))

    def test_unreadable_file_reason_is_readable(self) -> None:
        items = [i for i in self.load() if "S02E02" in i.path]
        self.run_batch(items)
        self.assertEqual(items[0].status, "failed")
        self.assertIn("Not a readable video file", items[0].reason)

    def test_stalled_ffmpeg_is_killed_and_marked_failed(self) -> None:
        big = self.tmp / "src" / "Big.Show.S01E01.avi"
        make_avi(big, seconds=40, size="1280x720")
        items = self.load([str(big)])
        res = self.run_batch(items, stall_timeout=0.0)
        self.assertEqual(items[0].status, "failed")
        self.assertIn("no progress", items[0].reason)
        self.assertEqual(res.counts["failed"], 1)
        self.no_part_files()

    def test_plan_and_estimate(self) -> None:
        items = self.load()
        plans = batch.plan_items(items, "The Show", "2004", str(self.out))
        actions = [plans[id(i)].action for i in items]
        self.assertEqual(actions, ["reencode", "reencode", "remux", "fail"])
        rates = batch.default_rates()
        self.assertGreater(batch.estimate_item(items[0].info, rates), 0)
        bench = batch.benchmark_encode(items[0].path, items[0].info, FFMPEG, sample_seconds=2)
        self.assertIsNotNone(bench)
        self.assertGreater(bench, 0)


@unittest.skipUnless(FFMPEG and FFPROBE, "ffmpeg not available")
class TestAllTracksKept(unittest.TestCase):
    """Dual audio + multiple subtitle tracks must survive both remux and re-encode."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="plextracks_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def convert(self, src: Path) -> Path:
        out = self.tmp / "out" / (src.stem + ".mkv")
        convert(str(src), str(out), probe(str(src), FFPROBE), FFMPEG, ffprobe_exe=FFPROBE)
        self.assertEqual([p.name for p in out.parent.iterdir()], [out.name])  # no leftovers
        return out

    def assert_same_timing(self, src: Path, out: Path, stream: int) -> None:
        before, after = subtitle_times(src, stream), subtitle_times(out, stream)
        self.assertEqual(len(before), len(after))
        for (s0, d0), (s1, d1) in zip(before, after):
            self.assertLessEqual(abs(s0 - s1), 50, (before, after))
            self.assertLessEqual(abs(d0 - d1), 50, (before, after))

    def test_avi_dual_audio_and_divx_subtitles_reencode(self) -> None:
        src = self.tmp / "Show.S01E01.avi"
        media.make_dual_xsub_avi(FFMPEG, src)
        self.assertEqual([t[1] for t in tracks(src)], ["mpeg4", "mp3", "mp3", "xsub", "xsub"])
        out = self.convert(src)
        self.assertEqual([t[1] for t in tracks(out)],
                         ["h264", "mp3", "mp3", "dvd_subtitle", "dvd_subtitle"])
        self.assert_same_timing(src, out, 0)
        self.assert_same_timing(src, out, 1)

    def test_mkv_remux_keeps_languages_styles_and_fonts(self) -> None:
        src = self.tmp / "Show.S01E02.mkv"
        media.make_dual_mkv(FFMPEG, src, FONT if FONT.exists() else None)
        out = self.convert(src)
        got = tracks(out)
        self.assertEqual(got[:5], [("video", "h264", ""), ("audio", "aac", "eng"),
                                   ("audio", "aac", "jpn"), ("subtitle", "subrip", "eng"),
                                   ("subtitle", "ass", "jpn")])
        if FONT.exists():
            self.assertEqual(got[5][0], "attachment")
        self.assert_same_timing(src, out, 1)

    def test_mp4_text_subtitles_become_srt(self) -> None:
        src = self.tmp / "Show.S01E03.mp4"
        media.make_mov_text_mp4(FFMPEG, src)
        out = self.convert(src)
        self.assertEqual([t[1] for t in tracks(out)],
                         ["h264", "aac", "aac", "subrip", "subrip"])
        self.assert_same_timing(src, out, 0)

    def test_missing_track_in_output_is_caught_and_not_saved(self) -> None:
        src = self.tmp / "Show.S01E04.mkv"
        media.make_dual_mkv(FFMPEG, src, None)
        out = self.tmp / "out" / "x.mkv"
        real = converter.build_cmd

        def drop_second_audio(*a, **kw):
            cmd = real(*a, **kw)
            i = cmd.index("0:2")  # the jpn audio stream's -map
            return cmd[:i - 1] + cmd[i + 1:]

        with unittest.mock.patch.object(converter, "build_cmd", drop_second_audio):
            with self.assertRaisesRegex(RuntimeError, "missing tracks"):
                convert(str(src), str(out), probe(str(src), FFPROBE), FFMPEG,
                        ffprobe_exe=FFPROBE)
        self.assertFalse(out.exists())
        self.assertFalse(Path(str(out) + PART_SUFFIX).exists())

    def test_unstorable_subtitles_refused_up_front(self) -> None:
        info = VideoInfo("h264", "", 320, 240, 4, False, streams=[
            StreamInfo(0, "video", "h264"), StreamInfo(1, "audio", "aac"),
            StreamInfo(2, "subtitle", "dvb_teletext", "deu")])
        out = self.tmp / "x.mkv"
        with self.assertRaisesRegex(RuntimeError, "can't store"):
            convert("does-not-matter.ts", str(out), info, FFMPEG)
        it = batch.BatchItem.from_path(str(self.tmp / "Show.S01E01.ts"), str(self.tmp))
        it.info = info
        plan = batch.plan_items([it], "Show", None, str(self.tmp / "o"))[id(it)]
        self.assertEqual((plan.action, plan.needs_attention), ("fail", True))
        self.assertIn("dvb_teletext", plan.note)

    def test_build_cmd_maps_every_track(self) -> None:
        info = VideoInfo("mpeg4", "", 640, 480, 10, True, streams=[
            StreamInfo(0, "video", "mpeg4"), StreamInfo(1, "audio", "mp3"),
            StreamInfo(2, "audio", "ac3"), StreamInfo(3, "subtitle", "xsub"),
            StreamInfo(4, "subtitle", "subrip"), StreamInfo(5, "subtitle", "xsub"),
            StreamInfo(6, "video", "mjpeg", attached_pic=True), StreamInfo(7, "data", "bin_data")])
        cmd = converter.build_cmd("in.avi", "out.mkv.part", info, "ffmpeg", "out.mkv" + SUBS_SUFFIX)
        maps = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-map"]
        self.assertEqual(maps, ["0:0", "0:1", "0:2", "1:s:0", "0:4", "1:s:1"])
        self.assertIn("-c:s:0", cmd)
        self.assertEqual(cmd[cmd.index("-c:s:0") + 1], "dvd_subtitle")
        self.assertEqual(cmd[cmd.index("-c:s:2") + 1], "dvd_subtitle")
        self.assertNotIn("-c:s:1", cmd)


@unittest.skipUnless(FFMPEG and FFPROBE, "ffmpeg not available")
class TestKeepOriginalNames(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="plexkeep_"))
        self.src = self.tmp / "Shorts"
        self.out = self.tmp / "out"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def items(self) -> list[batch.BatchItem]:
        items = [batch.BatchItem.from_path(p, r) for p, r in batch.collect_videos([str(self.src)])]
        for it in items:
            it.info = probe(it.path, FFPROBE)
        return items

    def test_names_kept_exactly_and_output_is_flat(self) -> None:
        names = ["1993 - ShowName - Title.avi", "1995 - ShowName - Another One.avi"]
        make_avi(self.src / names[0])
        make_avi(self.src / "more" / names[1], codec="h264")
        items = self.items()
        plans = batch.plan_items(items, "", None, str(self.out), keep_names=True)
        self.assertEqual([plans[id(i)].action for i in items], ["reencode", "remux"])
        self.assertFalse(any(p.needs_attention for p in plans.values()))

        done = threading.Event()
        result = []
        runner = batch.BatchRunner(items, "", None, str(self.out), FFMPEG, FFPROBE,
                                   str(self.tmp / "logs"), batch.default_rates(),
                                   on_finished=lambda r: (result.append(r), done.set()),
                                   keep_names=True)
        runner.start()
        self.assertTrue(done.wait(120))
        self.assertEqual(result[0].counts["converted"], 2)
        self.assertEqual(sorted(p.name for p in self.out.iterdir()),
                         ["1993 - ShowName - Title.mkv", "1995 - ShowName - Another One.mkv"])
        self.assertIn("keeping original filenames",
                      Path(result[0].summary_path).read_text(encoding="utf-8"))

        # Re-run: recognised as done.
        items = self.items()
        plans = batch.plan_items(items, "", None, str(self.out), keep_names=True)
        self.assertEqual({p.note for p in plans.values()}, {"Already converted"})

    def test_same_name_in_two_folders_is_flagged(self) -> None:
        make_avi(self.src / "a" / "Clip.avi")
        make_avi(self.src / "b" / "Clip.avi")
        items = self.items()
        plans = batch.plan_items(items, "", None, str(self.out), keep_names=True)
        self.assertTrue(all(p.needs_attention and p.action == "skip" for p in plans.values()))
        self.assertIn("Same output name", plans[id(items[0])].note)

    def test_flat_output_path_changes_only_extension(self) -> None:
        self.assertEqual(flat_output_path(r"D:\x\1993 - Show - It's a Title.v2.avi", r"E:\out"),
                         r"E:\out\1993 - Show - It's a Title.v2.mkv")


class TestLegacyRename(unittest.TestCase):
    def setUp(self) -> None:
        self.lib = Path(tempfile.mkdtemp(prefix="plexlegacy_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.lib, ignore_errors=True)

    def touch(self, rel: str) -> Path:
        p = self.lib / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
        return p

    def test_dry_run_then_apply(self) -> None:
        self.touch(r"The Wire (2002)\Season 01\The Wire - S01E01.mkv")
        self.touch(r"The Wire (2002)\Season 01\The Wire - S01E02.mkv")
        self.touch(r"No Year\Season 02\No Year - S02E10.mkv")          # case-only change
        self.touch(r"Other (2001)\Season 01\Wrong Title - S01E01.mkv")  # doesn't match folder
        self.touch(r"Dup (2003)\Season 01\Dup - S01E01.mkv")
        self.touch(r"Dup (2003)\Season 01\Dup (2003) - s01e01.mkv")    # new name exists
        self.touch(r"The Wire (2002)\Season 01\notes.mkv")              # not old format

        with unittest.mock.patch.object(legacy, "LOG_DIR", self.lib / "_logs"), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(legacy.main([str(self.lib)]), 0)  # dry run
            self.assertTrue((self.lib / r"The Wire (2002)\Season 01\The Wire - S01E01.mkv").exists())
            self.assertEqual(legacy.main([str(self.lib), "--apply"]), 0)

        names = sorted(str(p.relative_to(self.lib)) for p in self.lib.rglob("*.mkv"))
        self.assertEqual(names, [
            r"Dup (2003)\Season 01\Dup (2003) - s01e01.mkv",
            r"Dup (2003)\Season 01\Dup - S01E01.mkv",
            r"No Year\Season 02\No Year - s02e10.mkv",
            r"Other (2001)\Season 01\Wrong Title - S01E01.mkv",
            r"The Wire (2002)\Season 01\The Wire (2002) - s01e01.mkv",
            r"The Wire (2002)\Season 01\The Wire (2002) - s01e02.mkv",
            r"The Wire (2002)\Season 01\notes.mkv",
        ])
        undo = list((self.lib / "_logs").glob("renamed_*.csv"))
        self.assertEqual(len(undo), 1)
        self.assertEqual(len(undo[0].read_text(encoding="utf-8").strip().splitlines()), 4)

    def test_renamed_names_match_what_batch_expects(self) -> None:
        self.touch(r"The Wire (2002)\Season 03\The Wire - S03E07.mkv")
        legacy.apply_renames(legacy.plan_renames(self.lib))
        expected = Path(plex_tv_path("The Wire", "2002", 3, 7, str(self.lib)))
        self.assertTrue(expected.exists())
        self.assertEqual(expected.name, os.listdir(expected.parent)[0])  # exact case too


if __name__ == "__main__":
    unittest.main()

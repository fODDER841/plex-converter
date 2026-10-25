"""
Failure-path tests for first-run FFmpeg setup.

Each test runs in a sandbox that looks like a machine without FFmpeg: PATH,
LOCALAPPDATA and PROGRAMFILES point into an empty temp dir and settings are
isolated. A fake `winget.bat` (on the sandbox PATH) mimics the real CLI —
streaming \\r-redrawn progress and laying files out exactly like the real
Gyan.FFmpeg package — so the real find_winget() / install() / resolve() chain
is exercised end to end.

Run:  python -m unittest tests.test_ffmpeg_setup -v
Set SETUP_SCREENSHOTS=<dir> to save a PNG of each dialog state.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import textwrap
import time
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app as app_mod  # noqa: E402
import src.config as config  # noqa: E402
import src.ffmpeg_finder as finder  # noqa: E402
import src.ffmpeg_installer as installer  # noqa: E402

PKG_DIR = r"Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe"

FAKE_WINGET_PY = textwrap.dedent(r'''
    import os, sys, time
    from pathlib import Path
    args = sys.argv[1:]
    if args == ["--version"]:
        print("v1.29.380"); sys.exit(0)
    Path(os.environ["FAKE_WINGET_ARGS"]).write_text(" ".join(args))
    mode = os.environ.get("FAKE_WINGET_MODE", "success")
    out = sys.stdout
    out.write("Found FFmpeg [Gyan.FFmpeg] Version 9.0.2\r\n")
    if mode == "fail":
        out.write("An unexpected error occurred while executing the command:\r\n")
        out.write("InternetOpenUrl() failed.\r\n0x80072ee7 : unknown error\r\n")
        out.flush(); sys.exit(1)
    out.write("Downloading https://github.com/GyanD/codexffmpeg/releases/ffmpeg-9.0.2-full_build.zip\r\n")
    for mb in range(0, 101, 20):
        bar = "\u2588" * (mb // 10) + "\u2592" * (10 - mb // 10)
        out.write(f"\r  {bar}  {mb:.1f} MB / 100 MB"); out.flush(); time.sleep(0.15)
    out.write("\r\nSuccessfully verified installer hash\r\nExtracting archive...\r\n")
    out.write("Successfully extracted archive\r\nStarting package install...\r\n")
    out.flush(); time.sleep(0.3)
    if mode == "success":
        bin_dir = Path(os.environ["LOCALAPPDATA"]) / r"PKG_DIR" / "ffmpeg-9.0.2-full_build" / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        for n in ("ffmpeg.exe", "ffprobe.exe"):
            (bin_dir / n).write_bytes(b"")
    out.write("Successfully installed\r\n")
    sys.exit(0)
''').replace("PKG_DIR", PKG_DIR)


def pump(app, until, timeout: float = 15.0) -> None:
    # Run the real mainloop in short slices: worker threads marshal results via
    # widget.after(), which tkinter only accepts while mainloop is running.
    end = time.time() + timeout
    while time.time() < end:
        app.after(50, app.quit)
        app.mainloop()
        if until():
            return
    raise AssertionError("timed out waiting for UI condition")


class SetupSandbox(unittest.TestCase):
    with_winget = True
    winget_mode = "success"

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="plexconv_test_"))
        (self.tmp / "local").mkdir()
        (self.tmp / "progfiles").mkdir()
        bin_dir = self.tmp / "fakebin"
        bin_dir.mkdir()
        self.args_file = self.tmp / "winget_args.txt"

        path = [os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")]
        if self.with_winget:
            (bin_dir / "fake_winget.py").write_text(FAKE_WINGET_PY, encoding="utf-8")
            (bin_dir / "winget.bat").write_text(
                f'@"{sys.executable}" -u "%~dp0fake_winget.py" %*\r\n'
                "@exit /b %ERRORLEVEL%\r\n"
            )
            path.insert(0, str(bin_dir))

        env = {
            "PATH": os.pathsep.join(path),
            "LOCALAPPDATA": str(self.tmp / "local"),
            "PROGRAMFILES": str(self.tmp / "progfiles"),
            "FAKE_WINGET_ARGS": str(self.args_file),
            "FAKE_WINGET_MODE": self.winget_mode,
            "PYTHONIOENCODING": "utf-8",  # real winget emits UTF-8 bar glyphs
        }
        patches = [
            mock.patch.dict(os.environ, env),
            mock.patch.object(config, "_DIR", self.tmp / "cfg"),
            mock.patch.object(config, "_FILE", self.tmp / "cfg" / "settings.json"),
            mock.patch.object(finder, "_COMMON_DIRS", []),
            # The real registry PATH on a dev box contains ffmpeg; keep it out.
            mock.patch.object(finder, "refresh_path_from_registry", lambda: None),
            mock.patch.object(app_mod.webbrowser, "open", self._record_url),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.opened_urls: list[str] = []

        # Sanity: the sandbox really has no ffmpeg.
        self.assertEqual(finder.resolve(None)[0], None)

        self.app = app_mod.App()
        self.addCleanup(self._teardown_app)

    def _record_url(self, url: str) -> bool:
        self.opened_urls.append(url)
        return True

    def _teardown_app(self) -> None:
        try:
            self.app.destroy()
        except Exception:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------ helpers

    @property
    def dlg(self) -> app_mod.FFmpegSetupDialog:
        d = self.app._setup_dialog
        self.assertIsNotNone(d, "setup dialog was not opened")
        return d

    def wait_view(self, view: str, timeout: float = 15.0) -> None:
        pump(self.app, lambda: self.app._setup_dialog is not None
             and self.app._setup_dialog._view == view, timeout)
        self.app.update()
        self.screenshot(view)

    def screenshot(self, name: str) -> None:
        out = os.environ.get("SETUP_SCREENSHOTS")
        if not out:
            return
        from PIL import ImageGrab
        d = self.app._setup_dialog
        d.update()
        time.sleep(0.2)
        d.update()
        box = (d.winfo_rootx(), d.winfo_rooty(),
               d.winfo_rootx() + d.winfo_width(), d.winfo_rooty() + d.winfo_height())
        Path(out).mkdir(parents=True, exist_ok=True)
        ImageGrab.grab(bbox=box, all_screens=True).save(
            Path(out) / f"{type(self).__name__}_{self._testMethodName}_{name}.png")

    def assert_gated(self) -> None:
        self.assertIn("can't be converted", self.app._drop_label.cget("text"))

    def assert_ready(self) -> None:
        self.assertIsNotNone(self.app._ffmpeg_exe)
        self.assertIsNotNone(self.app._ffprobe_exe)
        self.assertEqual(self.app._drop_label.cget("text"),
                         "Drop a video file here\nor click to browse")


class TestMissingWithWinget(SetupSandbox):
    def test_dialog_appears_and_app_is_gated(self) -> None:
        self.wait_view("choose")
        self.assert_gated()
        self.assertTrue(self.dlg._install_btn.winfo_ismapped())

        # Dropping a file while gated re-focuses setup instead of erroring,
        # and doesn't stack a second dialog.
        first = self.dlg
        with mock.patch.object(app_mod.messagebox, "showerror") as err:
            self.app._on_drop(types.SimpleNamespace(data=r"{C:\videos\movie.mkv}"))
            self.app._start_conversion()
            err.assert_not_called()
        self.assertIs(self.app._setup_dialog, first)

        # "Not now" closes the dialog but the app stays gated; clicking
        # the drop zone brings setup back.
        self.dlg._close_btn.invoke()
        self.app.update()
        self.assertFalse(first.winfo_exists())
        self.assert_gated()
        self.app._browse_file()
        self.wait_view("choose")
        self.assertIsNot(self.app._setup_dialog, first)

    def test_install_button_installs_without_restart(self) -> None:
        self.wait_view("choose")
        progress: list[installer.Progress] = []
        orig = self.dlg._on_progress
        self.dlg._on_progress = lambda p: (progress.append(p), orig(p))

        self.dlg._install_btn.invoke()
        self.wait_view("installing", timeout=2)
        self.assertEqual(str(self.dlg._close_btn.cget("state")), "disabled")
        self.dlg._close()  # closing mid-install is refused
        self.assertTrue(self.dlg.winfo_exists())

        pump(self.app, lambda: any(p.fraction and 0 < p.fraction < 1 for p in progress))
        self.screenshot("installing_progress")
        self.wait_view("success", timeout=30)

        self.assertEqual(self.args_file.read_text().split(), installer.INSTALL_ARGS)
        self.assertTrue(any(p.stage == "downloading" and p.fraction == 1.0 for p in progress))
        self.assertTrue(any(p.stage == "installing" for p in progress))
        self.assertIn(str(self.tmp / "local"), self.app._ffmpeg_exe)
        self.assert_ready()  # same process, no restart

        self.dlg._close_btn.invoke()
        self.app.update()
        self.assert_ready()


class TestInstallFails(SetupSandbox):
    winget_mode = "fail"

    def test_failure_shows_retry_and_details(self) -> None:
        self.wait_view("choose")
        self.dlg._install_btn.invoke()
        self.wait_view("error", timeout=30)
        self.assert_gated()
        self.assertEqual(self.dlg._install_btn.cget("text"), "Try again")
        self.assertEqual(str(self.dlg._close_btn.cget("state")), "normal")
        # Retry runs the installer again.
        self.args_file.unlink()
        self.dlg._install_btn.invoke()
        self.wait_view("error", timeout=30)
        self.assertTrue(self.args_file.exists())


class TestInstallNeedsRestart(SetupSandbox):
    winget_mode = "invisible"  # winget reports success but files aren't findable

    def test_restart_message(self) -> None:
        self.wait_view("choose")
        self.dlg._install_btn.invoke()
        self.wait_view("restart", timeout=30)
        self.assertEqual(self.dlg._close_btn.cget("text"), "Close Plex Converter")


class TestNoWinget(SetupSandbox):
    with_winget = False

    def test_no_winget_offers_links(self) -> None:
        self.assertIsNone(installer.find_winget())
        self.wait_view("no_winget")
        self.assert_gated()
        self.assertFalse(hasattr(self.dlg, "_install_btn"))
        self.dlg._recheck_btn.invoke()
        self.wait_view("no_winget")

    def test_browse_bad_then_good_folder(self) -> None:
        self.wait_view("no_winget")
        empty = self.tmp / "empty"
        empty.mkdir()
        # A typical unzipped download: <root>/ffmpeg-x-full_build/bin/*.exe
        bin_dir = self.tmp / "Downloads" / "ffmpeg-9.0.2-full_build" / "bin"
        bin_dir.mkdir(parents=True)
        for n in ("ffmpeg.exe", "ffprobe.exe"):
            (bin_dir / n).write_bytes(b"")

        with mock.patch.object(app_mod.filedialog, "askdirectory", return_value=str(empty)):
            self.dlg._browse_btn.invoke()
        self.wait_view("no_winget")
        self.assertIsNotNone(self.dlg._browse_note)
        self.assert_gated()

        with mock.patch.object(app_mod.filedialog, "askdirectory",
                               return_value=str(self.tmp / "Downloads")):
            self.dlg._browse_btn.invoke()
        self.wait_view("success")
        self.assert_ready()
        self.assertEqual(config.load()["ffmpeg_dir"], str(bin_dir))


class TestInstallerUnits(unittest.TestCase):
    def test_parse_progress(self) -> None:
        p = installer.parse_progress("████▒▒▒▒  45.0 MB / 90.0 MB")
        self.assertEqual((p.stage, p.fraction), ("downloading", 0.5))
        p = installer.parse_progress("1.00 GB / 2.00 GB")
        self.assertAlmostEqual(p.fraction, 0.5)
        self.assertEqual(installer.parse_progress("Extracting archive...").stage, "installing")
        self.assertIsNone(installer.parse_progress("Found FFmpeg [Gyan.FFmpeg]"))

    def test_clean_strips_spinner_and_bar(self) -> None:
        self.assertEqual(installer._clean("-\x08\\\x08|\x08  ██▒▒ 3 MB / 9 MB"), "3 MB / 9 MB")

    @unittest.skipUnless(os.environ.get("RUN_REAL_WINGET"), "set RUN_REAL_WINGET=1")
    def test_real_winget_invocation(self) -> None:
        exe = installer.find_winget()
        self.assertIsNotNone(exe)
        result = installer.install(exe)
        print(f"\nreal winget rc={result.returncode:#x}\n{result.output}")
        self.assertIsNotNone(finder.resolve(None)[0])


if __name__ == "__main__":
    unittest.main()

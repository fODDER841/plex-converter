"""Plex Converter — drag-and-drop video-to-MKV conversion tool."""
from __future__ import annotations

import os
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk
import tkinterdnd2

import src.config as config
import src.ffmpeg_finder as finder
from src.converter import build_cmd, convert
from src.naming import ParsedName, parse_filename, plex_movie_path, plex_tv_path
from src.probe import VideoInfo, probe

ctk.set_appearance_mode("System")
ctk.set_default_color_theme("blue")

_DARK_BG = "#2b2b2b"
_LIGHT_BG = "#ebebeb"

VIDEO_EXTENSIONS = (
    ".mp4", ".mkv", ".avi", ".mov", ".wmv",
    ".m2ts", ".ts", ".m4v", ".flv", ".vob", ".mpg", ".mpeg",
)


def _window_bg() -> str:
    return _DARK_BG if ctk.get_appearance_mode() == "Dark" else _LIGHT_BG


# ---------------------------------------------------------------------------
# Diagnostics window
# ---------------------------------------------------------------------------

class DiagnosticsWindow(ctk.CTkToplevel):
    def __init__(self, parent: "App") -> None:
        super().__init__(parent)
        self.title("Diagnostics")
        self.resizable(False, False)
        self.grab_set()

        pad = {"padx": 16, "pady": 6}

        ctk.CTkLabel(self, text="Diagnostics", font=ctk.CTkFont(size=16, weight="bold")).pack(
            pady=(16, 8)
        )

        # --- binaries ---
        bin_frame = ctk.CTkFrame(self)
        bin_frame.pack(fill="x", padx=16, pady=(0, 8))

        ctk.CTkLabel(bin_frame, text="FFmpeg Binaries", font=ctk.CTkFont(weight="bold")).pack(
            anchor="w", **pad
        )

        def _row(label: str, value: str) -> None:
            f = ctk.CTkFrame(bin_frame, fg_color="transparent")
            f.pack(fill="x", padx=16, pady=2)
            ctk.CTkLabel(f, text=label, width=90, anchor="e", text_color="gray60").pack(
                side="left", padx=(0, 8)
            )
            ctk.CTkLabel(f, text=value, anchor="w", wraplength=400).pack(side="left")

        app = parent
        if app._ffmpeg_exe:
            _row("ffmpeg:", app._ffmpeg_exe)
            _row("ffprobe:", app._ffprobe_exe or "(not set)")
            _row("source:", app._ffmpeg_source)
            _row("version:", finder.get_version(app._ffmpeg_exe))
        else:
            _row("status:", "NOT FOUND — use FFmpeg Settings to configure")

        # --- last command ---
        cmd_frame = ctk.CTkFrame(self)
        cmd_frame.pack(fill="x", padx=16, pady=(0, 16))

        ctk.CTkLabel(cmd_frame, text="Last Command", font=ctk.CTkFont(weight="bold")).pack(
            anchor="w", **pad
        )

        cmd_text = app._last_cmd or "(no conversion run yet)"
        box = ctk.CTkTextbox(cmd_frame, height=120, width=500, wrap="word")
        box.pack(padx=16, pady=(0, 8))
        box.insert("1.0", cmd_text)
        box.configure(state="disabled")

        ctk.CTkButton(self, text="Close", width=100, command=self.destroy).pack(pady=(0, 16))


# ---------------------------------------------------------------------------
# FFmpeg settings dialog
# ---------------------------------------------------------------------------

class FFmpegSettingsDialog(ctk.CTkToplevel):
    def __init__(self, parent: "App") -> None:
        super().__init__(parent)
        self.title("FFmpeg Settings")
        self.resizable(False, False)
        self.grab_set()
        self._parent = parent

        ctk.CTkLabel(
            self, text="FFmpeg Settings", font=ctk.CTkFont(size=16, weight="bold")
        ).pack(pady=(16, 4))

        ctk.CTkLabel(
            self,
            text=(
                "Locate the folder that contains ffmpeg.exe and ffprobe.exe.\n"
                "Leave blank to use auto-detection (PATH, WinGet, common locations)."
            ),
            text_color="gray60",
            wraplength=440,
        ).pack(pady=(0, 12))

        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(padx=16, pady=(0, 8), fill="x")

        ctk.CTkLabel(row, text="Folder:", width=60, anchor="e").pack(side="left", padx=(0, 8))
        self._dir_var = tk.StringVar(value=parent._cfg.get("ffmpeg_dir", ""))
        ctk.CTkEntry(row, textvariable=self._dir_var, width=320).pack(side="left", padx=(0, 6))
        ctk.CTkButton(row, text="Browse", width=80, command=self._browse).pack(side="left")

        self._status_label = ctk.CTkLabel(self, text="", wraplength=440)
        self._status_label.pack(pady=(0, 8))

        btn_row = ctk.CTkFrame(self, fg_color="transparent")
        btn_row.pack(pady=(0, 16))
        ctk.CTkButton(btn_row, text="Apply & Test", width=120, command=self._apply).pack(
            side="left", padx=6
        )
        ctk.CTkButton(
            btn_row, text="Cancel", width=80, fg_color="gray40", hover_color="gray30",
            command=self.destroy
        ).pack(side="left", padx=6)

        # Show current resolved state
        self._refresh_status()

    def _browse(self) -> None:
        d = filedialog.askdirectory(title="Select folder containing ffmpeg.exe")
        if d:
            self._dir_var.set(d)

    def _apply(self) -> None:
        configured = self._dir_var.get().strip() or None
        self._parent._cfg["ffmpeg_dir"] = configured or ""
        config.save(self._parent._cfg)
        self._parent._resolve_ffmpeg(configured_dir=configured)
        self._refresh_status()

    def _refresh_status(self) -> None:
        app = self._parent
        if app._ffmpeg_exe:
            self._status_label.configure(
                text=f"Found: {app._ffmpeg_exe}\nSource: {app._ffmpeg_source}",
                text_color="#3a9e3a",
            )
        else:
            self._status_label.configure(
                text="Not found. Browse to the folder containing ffmpeg.exe and ffprobe.exe.",
                text_color="#c0392b",
            )


# ---------------------------------------------------------------------------
# Main application
# ---------------------------------------------------------------------------

class App(tkinterdnd2.TkinterDnD.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Plex Converter")
        self.resizable(False, False)
        self.configure(bg=_window_bg())

        self._cfg = config.load()

        # ffmpeg state
        self._ffmpeg_exe: str | None = None
        self._ffprobe_exe: str | None = None
        self._ffmpeg_source: str = "not found"

        # conversion state
        self._input_file: str | None = None
        self._video_info: VideoInfo | None = None
        self._cancel = threading.Event()
        self._last_cmd: str | None = None

        self._build_ui()
        self._set_state("idle")

        # Resolve ffmpeg; show setup dialog if missing.
        self.after(100, self._initial_ffmpeg_check)

    # ------------------------------------------------------------------ ffmpeg

    def _resolve_ffmpeg(self, configured_dir: str | None = None) -> None:
        if configured_dir is None:
            configured_dir = self._cfg.get("ffmpeg_dir") or None
        ffmpeg, ffprobe, source = finder.resolve(configured_dir)
        self._ffmpeg_exe = ffmpeg
        self._ffprobe_exe = ffprobe
        self._ffmpeg_source = source
        self._update_ffmpeg_indicator()

    def _initial_ffmpeg_check(self) -> None:
        self._resolve_ffmpeg()
        if not self._ffmpeg_exe:
            FFmpegSettingsDialog(self)

    def _update_ffmpeg_indicator(self) -> None:
        if self._ffmpeg_exe:
            short = Path(self._ffmpeg_exe).parent.name
            self._ffmpeg_status_label.configure(
                text=f"ffmpeg: {short}", text_color="#3a9e3a"
            )
        else:
            self._ffmpeg_status_label.configure(
                text="ffmpeg: not found", text_color="#c0392b"
            )

    # ------------------------------------------------------------------ layout

    def _build_ui(self) -> None:
        outer = ctk.CTkFrame(self, corner_radius=0)
        outer.pack(fill="both", expand=True)

        # Title row
        title_row = ctk.CTkFrame(outer, fg_color="transparent")
        title_row.pack(fill="x", padx=16, pady=(14, 0))

        ctk.CTkLabel(
            title_row,
            text="Plex Converter",
            font=ctk.CTkFont(size=20, weight="bold"),
        ).pack(side="left")

        # Toolbar buttons (right-aligned in title row)
        ctk.CTkButton(
            title_row, text="Diagnostics", width=100, height=28,
            font=ctk.CTkFont(size=11),
            fg_color="gray30", hover_color="gray20",
            command=lambda: DiagnosticsWindow(self),
        ).pack(side="right", padx=(4, 0))

        ctk.CTkButton(
            title_row, text="FFmpeg Settings", width=120, height=28,
            font=ctk.CTkFont(size=11),
            fg_color="gray30", hover_color="gray20",
            command=lambda: FFmpegSettingsDialog(self),
        ).pack(side="right", padx=(0, 4))

        self._ffmpeg_status_label = ctk.CTkLabel(
            title_row, text="ffmpeg: checking...",
            font=ctk.CTkFont(size=11), text_color="gray60",
        )
        self._ffmpeg_status_label.pack(side="right", padx=8)

        # --- drop zone ---
        self._drop_frame = ctk.CTkFrame(
            outer, width=520, height=110, border_width=2, border_color="gray50"
        )
        self._drop_frame.pack(padx=30, pady=(10, 0))
        self._drop_frame.pack_propagate(False)

        self._drop_label = ctk.CTkLabel(
            self._drop_frame,
            text="Drop a video file here\nor click to browse",
            font=ctk.CTkFont(size=14),
            cursor="hand2",
        )
        self._drop_label.place(relx=0.5, rely=0.5, anchor="center")

        self._drop_frame.drop_target_register(tkinterdnd2.DND_FILES)
        self._drop_frame.dnd_bind("<<Drop>>", self._on_drop)
        self._drop_label.bind("<Button-1>", self._browse_file)
        self._drop_frame.bind("<Button-1>", self._browse_file)

        # --- codec info line ---
        self._info_label = ctk.CTkLabel(
            outer, text="", font=ctk.CTkFont(size=11), text_color="gray60"
        )

        # --- metadata form ---
        self._meta_frame = ctk.CTkFrame(outer)

        _lw = 80

        ctk.CTkLabel(self._meta_frame, text="Title:", width=_lw, anchor="e").grid(
            row=0, column=0, padx=(12, 6), pady=7, sticky="e"
        )
        self._title_var = tk.StringVar()
        ctk.CTkEntry(self._meta_frame, textvariable=self._title_var, width=360).grid(
            row=0, column=1, columnspan=3, padx=(0, 12), pady=7, sticky="ew"
        )

        ctk.CTkLabel(self._meta_frame, text="Year:", width=_lw, anchor="e").grid(
            row=1, column=0, padx=(12, 6), pady=7, sticky="e"
        )
        self._year_var = tk.StringVar()
        ctk.CTkEntry(self._meta_frame, textvariable=self._year_var, width=80).grid(
            row=1, column=1, padx=(0, 12), pady=7, sticky="w"
        )

        ctk.CTkLabel(self._meta_frame, text="Type:", width=_lw, anchor="e").grid(
            row=2, column=0, padx=(12, 6), pady=7, sticky="e"
        )
        self._type_var = tk.StringVar(value="movie")
        _radio_frame = ctk.CTkFrame(self._meta_frame, fg_color="transparent")
        _radio_frame.grid(row=2, column=1, columnspan=3, sticky="w")
        ctk.CTkRadioButton(
            _radio_frame, text="Movie", variable=self._type_var, value="movie",
            command=self._on_type_change,
        ).pack(side="left", padx=(0, 20))
        ctk.CTkRadioButton(
            _radio_frame, text="TV Show", variable=self._type_var, value="tv",
            command=self._on_type_change,
        ).pack(side="left")

        self._tv_row = ctk.CTkFrame(self._meta_frame, fg_color="transparent")
        ctk.CTkLabel(self._tv_row, text="Season:", width=_lw, anchor="e").pack(
            side="left", padx=(0, 6)
        )
        self._season_var = tk.StringVar()
        ctk.CTkEntry(self._tv_row, textvariable=self._season_var, width=60).pack(
            side="left", padx=(0, 20)
        )
        ctk.CTkLabel(self._tv_row, text="Episode:").pack(side="left", padx=(0, 6))
        self._episode_var = tk.StringVar()
        ctk.CTkEntry(self._tv_row, textvariable=self._episode_var, width=60).pack(side="left")

        ctk.CTkLabel(self._meta_frame, text="Output:", width=_lw, anchor="e").grid(
            row=4, column=0, padx=(12, 6), pady=7, sticky="e"
        )
        _out_frame = ctk.CTkFrame(self._meta_frame, fg_color="transparent")
        _out_frame.grid(row=4, column=1, columnspan=3, padx=(0, 12), pady=7, sticky="ew")
        self._output_var = tk.StringVar()
        ctk.CTkEntry(_out_frame, textvariable=self._output_var, width=290).pack(
            side="left", padx=(0, 6)
        )
        ctk.CTkButton(_out_frame, text="Browse", width=70, command=self._browse_output).pack(
            side="left"
        )

        self._meta_frame.columnconfigure(1, weight=1)

        # --- action buttons ---
        self._btn_frame = ctk.CTkFrame(outer, fg_color="transparent")
        self._convert_btn = ctk.CTkButton(
            self._btn_frame,
            text="Convert",
            width=200, height=42,
            font=ctk.CTkFont(size=14, weight="bold"),
            command=self._start_conversion,
        )
        self._convert_btn.pack(side="left", padx=6)
        self._cancel_btn = ctk.CTkButton(
            self._btn_frame,
            text="Cancel",
            width=100, height=42,
            fg_color="gray40", hover_color="gray30",
            command=self._cancel_conversion,
        )

        # --- progress area ---
        self._progress_frame = ctk.CTkFrame(outer, fg_color="transparent")
        self._progress_bar = ctk.CTkProgressBar(self._progress_frame, width=480)
        self._progress_bar.set(0)
        self._progress_bar.pack(pady=(0, 4))
        self._pct_label = ctk.CTkLabel(
            self._progress_frame, text="0%", font=ctk.CTkFont(size=12)
        )
        self._pct_label.pack()
        self._status_label = ctk.CTkLabel(
            self._progress_frame, text="", font=ctk.CTkFont(size=11),
            text_color="gray60", wraplength=480,
        )
        self._status_label.pack(pady=(4, 0))

    # ------------------------------------------------------------------ state

    def _set_state(self, state: str) -> None:
        self._info_label.pack_forget()
        self._meta_frame.pack_forget()
        self._btn_frame.pack_forget()
        self._progress_frame.pack_forget()
        self._cancel_btn.pack_forget()

        if state == "idle":
            self._drop_label.configure(text="Drop a video file here\nor click to browse")

        elif state == "loaded":
            self._info_label.pack(pady=(6, 0))
            self._meta_frame.pack(padx=30, pady=(6, 0), fill="x")
            self._btn_frame.pack(pady=(10, 16))

        elif state == "converting":
            self._progress_bar.set(0)
            self._pct_label.configure(text="0%")
            self._status_label.configure(text="Starting...", text_color="gray60")
            self._progress_frame.pack(padx=30, pady=(10, 6), fill="x")
            self._cancel_btn.pack(side="left", padx=6)
            self._btn_frame.pack(pady=(0, 16))
            self._convert_btn.configure(state="disabled")

        elif state in ("done", "error"):
            self._progress_frame.pack(padx=30, pady=(10, 6), fill="x")
            self._btn_frame.pack(pady=(0, 16))
            self._convert_btn.configure(
                state="normal",
                text="Convert Another",
                command=self._reset,
            )

    # ------------------------------------------------------------------ events

    def _on_drop(self, event: tk.Event) -> None:
        raw = event.data.strip()
        if raw.startswith("{") and raw.endswith("}"):
            raw = raw[1:-1]
        path = raw.split("} {")[0].strip("{}")
        self._load_file(path)

    def _browse_file(self, _event: tk.Event | None = None) -> None:
        ext_str = " ".join(f"*{e}" for e in VIDEO_EXTENSIONS)
        path = filedialog.askopenfilename(
            title="Select a video file",
            filetypes=[("Video files", ext_str), ("All files", "*.*")],
        )
        if path:
            self._load_file(path)

    def _browse_output(self) -> None:
        d = filedialog.askdirectory(title="Select output folder")
        if d:
            self._output_var.set(d)

    def _on_type_change(self) -> None:
        if self._type_var.get() == "tv":
            self._tv_row.grid(
                row=3, column=1, columnspan=3, sticky="w", padx=(0, 12), pady=4
            )
        else:
            self._tv_row.grid_remove()

    # ------------------------------------------------------------------ probe

    def _load_file(self, path: str) -> None:
        if not os.path.isfile(path):
            messagebox.showerror("File not found", f"Cannot read:\n{path}")
            return

        if not self._ffprobe_exe:
            messagebox.showerror(
                "ffprobe not found",
                "ffprobe is not configured.\n\nClick 'FFmpeg Settings' in the toolbar to locate the binaries.",
            )
            return

        ext = Path(path).suffix.lower()
        if ext not in VIDEO_EXTENSIONS:
            if not messagebox.askyesno(
                "Unsupported extension",
                f"'{ext}' is not a known video extension. Try anyway?",
            ):
                return

        self._input_file = path
        self._drop_label.configure(text="Inspecting file...")

        ffprobe_exe = self._ffprobe_exe  # capture for thread

        def _do_probe() -> None:
            try:
                info = probe(path, ffprobe_exe=ffprobe_exe)
                # Marshal result to main thread — never touch widgets from here.
                self.after(0, lambda: self._on_probe_done(path, info))
            except Exception as exc:
                err = str(exc)
                self.after(0, lambda: self._on_probe_error(err))

        threading.Thread(target=_do_probe, daemon=True).start()

    def _on_probe_done(self, path: str, info: VideoInfo) -> None:
        self._video_info = info
        self._drop_label.configure(text=Path(path).name)

        parsed: ParsedName = parse_filename(path)
        self._title_var.set(parsed.title)
        self._year_var.set(parsed.year or "")

        if parsed.is_tv:
            self._type_var.set("tv")
            self._season_var.set(str(parsed.season or 1))
            self._episode_var.set(str(parsed.episode or 1))
        else:
            self._type_var.set("movie")
        self._on_type_change()

        if not self._output_var.get():
            self._output_var.set(
                self._cfg.get("output_dir") or str(Path(path).parent)
            )

        op = info.operation_label
        self._info_label.configure(
            text=f"Codec: {info.codec}  |  {info.width}x{info.height}  |  {op}"
        )
        self._set_state("loaded")

    def _on_probe_error(self, msg: str) -> None:
        self._drop_label.configure(text="Drop a video file here\nor click to browse")
        messagebox.showerror("Probe failed", f"Could not inspect file:\n\n{msg}")

    # ------------------------------------------------------------------ convert

    def _start_conversion(self) -> None:
        if not self._ffmpeg_exe:
            messagebox.showerror(
                "ffmpeg not found",
                "ffmpeg is not configured.\n\nClick 'FFmpeg Settings' in the toolbar.",
            )
            return

        title = self._title_var.get().strip()
        year = self._year_var.get().strip() or None
        output_dir = self._output_var.get().strip()

        if not title:
            messagebox.showwarning("Missing title", "Please enter a title.")
            return
        if not output_dir:
            messagebox.showwarning("Missing output", "Please choose an output folder.")
            return

        is_tv = self._type_var.get() == "tv"
        if is_tv:
            try:
                season = int(self._season_var.get())
                episode = int(self._episode_var.get())
            except ValueError:
                messagebox.showwarning("Invalid", "Season and Episode must be numbers.")
                return
            output_path = plex_tv_path(title, year, season, episode, output_dir)
        else:
            output_path = plex_movie_path(title, year, output_dir)

        self._cfg["output_dir"] = output_dir
        config.save(self._cfg)

        # Store command for diagnostics before the thread starts.
        cmd = build_cmd(self._input_file, output_path, self._video_info, self._ffmpeg_exe)
        self._last_cmd = " ".join(f'"{a}"' if " " in a else a for a in cmd)

        self._set_state("converting")
        op = "Re-encoding" if self._video_info.needs_reencode else "Remuxing"
        self._status_label.configure(text=f"{op} to MKV...")

        self._cancel.clear()

        # Capture locals for the thread.
        src = self._input_file
        info = self._video_info
        ffmpeg_exe = self._ffmpeg_exe

        def _do_convert() -> None:
            try:
                convert(
                    src, output_path, info,
                    ffmpeg_exe=ffmpeg_exe,
                    progress_callback=lambda p: self.after(
                        0, lambda p=p: self._update_progress(p)
                    ),
                    cancel_event=self._cancel,
                )
                self.after(0, lambda: self._on_done(output_path))
            except Exception as exc:
                err = str(exc)
                self.after(0, lambda: self._on_error(err))

        threading.Thread(target=_do_convert, daemon=True).start()

    def _update_progress(self, fraction: float) -> None:
        self._progress_bar.set(fraction)
        self._pct_label.configure(text=f"{int(fraction * 100)}%")

    def _on_done(self, output_path: str) -> None:
        self._progress_bar.set(1.0)
        self._pct_label.configure(text="100%")
        self._status_label.configure(
            text=f"Done!  Saved to:\n{output_path}", text_color="#3a9e3a"
        )
        self._set_state("done")

    def _on_error(self, msg: str) -> None:
        short = msg[:400] + ("..." if len(msg) > 400 else "")
        self._status_label.configure(text=f"Error:\n{short}", text_color="#c0392b")
        self._set_state("error")

    def _cancel_conversion(self) -> None:
        self._cancel.set()
        self._status_label.configure(text="Cancelling...", text_color="orange")

    def _reset(self) -> None:
        self._input_file = None
        self._video_info = None
        self._output_var.set("")
        self._progress_bar.set(0)
        self._pct_label.configure(text="0%")
        self._status_label.configure(text="", text_color="gray60")
        self._convert_btn.configure(
            text="Convert", command=self._start_conversion, state="normal"
        )
        self._set_state("idle")


if __name__ == "__main__":
    app = App()
    app.mainloop()

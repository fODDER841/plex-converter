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
from src.converter import convert
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


class App(tkinterdnd2.TkinterDnD.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Plex Converter")
        self.resizable(False, False)
        self.configure(bg=_window_bg())

        self._cfg = config.load()
        self._input_file: str | None = None
        self._video_info: VideoInfo | None = None
        self._cancel = threading.Event()

        self._build_ui()
        self._set_state("idle")

    # ------------------------------------------------------------------ layout

    def _build_ui(self) -> None:
        outer = ctk.CTkFrame(self, corner_radius=0)
        outer.pack(fill="both", expand=True)

        ctk.CTkLabel(
            outer,
            text="Plex Converter",
            font=ctk.CTkFont(size=22, weight="bold"),
        ).pack(pady=(18, 6))

        # --- drop zone ---
        self._drop_frame = ctk.CTkFrame(
            outer, width=520, height=110, border_width=2, border_color="gray50"
        )
        self._drop_frame.pack(padx=30, pady=(6, 0))
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

        # --- codec info line (shown after probe) ---
        self._info_label = ctk.CTkLabel(
            outer,
            text="",
            font=ctk.CTkFont(size=11),
            text_color="gray60",
        )

        # --- metadata form ---
        self._meta_frame = ctk.CTkFrame(outer)

        _lw = 80  # label column width

        # Title
        ctk.CTkLabel(self._meta_frame, text="Title:", width=_lw, anchor="e").grid(
            row=0, column=0, padx=(12, 6), pady=7, sticky="e"
        )
        self._title_var = tk.StringVar()
        ctk.CTkEntry(self._meta_frame, textvariable=self._title_var, width=360).grid(
            row=0, column=1, columnspan=3, padx=(0, 12), pady=7, sticky="ew"
        )

        # Year
        ctk.CTkLabel(self._meta_frame, text="Year:", width=_lw, anchor="e").grid(
            row=1, column=0, padx=(12, 6), pady=7, sticky="e"
        )
        self._year_var = tk.StringVar()
        ctk.CTkEntry(self._meta_frame, textvariable=self._year_var, width=80).grid(
            row=1, column=1, padx=(0, 12), pady=7, sticky="w"
        )

        # Content type
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

        # Season / Episode (TV only — toggled by _on_type_change)
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
        ctk.CTkEntry(self._tv_row, textvariable=self._episode_var, width=60).pack(
            side="left"
        )

        # Output folder
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
            width=200,
            height=42,
            font=ctk.CTkFont(size=14, weight="bold"),
            command=self._start_conversion,
        )
        self._convert_btn.pack(side="left", padx=6)
        self._cancel_btn = ctk.CTkButton(
            self._btn_frame,
            text="Cancel",
            width=100,
            height=42,
            fg_color="gray40",
            hover_color="gray30",
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
            self._progress_frame,
            text="",
            font=ctk.CTkFont(size=11),
            text_color="gray60",
            wraplength=480,
        )
        self._status_label.pack(pady=(4, 0))

    # ------------------------------------------------------------------ state

    def _set_state(self, state: str) -> None:
        """Show/hide UI sections based on the current workflow state."""
        # Hide everything that isn't always shown.
        self._info_label.pack_forget()
        self._meta_frame.pack_forget()
        self._btn_frame.pack_forget()
        self._progress_frame.pack_forget()
        self._cancel_btn.pack_forget()

        if state == "idle":
            self._drop_label.configure(
                text="Drop a video file here\nor click to browse"
            )

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

        elif state == "done":
            self._progress_frame.pack(padx=30, pady=(10, 6), fill="x")
            self._btn_frame.pack(pady=(0, 16))
            self._convert_btn.configure(
                state="normal", text="Convert Another", command=self._reset
            )

        elif state == "error":
            self._progress_frame.pack(padx=30, pady=(10, 6), fill="x")
            self._btn_frame.pack(pady=(0, 16))
            self._convert_btn.configure(
                state="normal", text="Try Again", command=self._reset
            )

    # ------------------------------------------------------------------ events

    def _on_drop(self, event: tk.Event) -> None:
        raw = event.data.strip()
        # tkinterdnd2 wraps paths with spaces in curly braces on Windows.
        if raw.startswith("{") and raw.endswith("}"):
            raw = raw[1:-1]
        # Multiple files: take only the first.
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
            self._tv_row.grid(row=3, column=1, columnspan=3, sticky="w", padx=(0, 12), pady=4)
        else:
            self._tv_row.grid_remove()

    # ------------------------------------------------------------------ probe

    def _load_file(self, path: str) -> None:
        if not os.path.isfile(path):
            messagebox.showerror("File not found", f"Cannot read:\n{path}")
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

        def _do_probe() -> None:
            try:
                info = probe(path)
                self.after(0, lambda: self._on_probe_done(path, info))
            except Exception as exc:
                self.after(0, lambda: self._on_probe_error(str(exc)))

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

        # Default output dir: saved setting, or folder next to the source file.
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

        # Persist output dir.
        self._cfg["output_dir"] = output_dir
        config.save(self._cfg)

        self._set_state("converting")
        op = "Re-encoding" if self._video_info.needs_reencode else "Remuxing"
        self._status_label.configure(text=f"{op} to MKV...")

        self._cancel.clear()
        info = self._video_info
        src = self._input_file

        def _do_convert() -> None:
            try:
                convert(
                    src,
                    output_path,
                    info,
                    progress_callback=lambda p: self.after(0, lambda: self._update_progress(p)),
                    cancel_event=self._cancel,
                )
                self.after(0, lambda: self._on_done(output_path))
            except Exception as exc:
                self.after(0, lambda: self._on_error(str(exc)))

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

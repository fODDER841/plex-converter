"""Batch conversion panel: queue list, batch-wide metadata, progress, summary."""
from __future__ import annotations

import os
import re
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import TYPE_CHECKING, Callable, Optional

import customtkinter as ctk
import tkinterdnd2

import src.config as config
from src import batch
from src.batch import (CONVERTING, DONE, FAILED, QUEUED, SKIPPED, BatchItem, BatchResult,
                       BatchRunner, Plan, Rates)
from src.probe import probe

if TYPE_CHECKING:
    from app import App

_RED = "#c0392b"
_ORANGE = "#d68910"

_ACTION_TEXT = {"reencode": "Re-encode", "remux": "Remux", "skip": "—", "fail": "—",
                "probing": "…"}


def _rates_from_cfg(cfg: dict) -> Rates:
    saved = cfg.get("batch_rates") or {}
    r = batch.default_rates()
    try:
        r.encode_px_per_sec = float(saved["encode_px_per_sec"])
        r.remux_bytes_per_sec = float(saved["remux_bytes_per_sec"])
        r.source = saved.get("source", "test encode")
    except (KeyError, TypeError, ValueError):
        pass
    return r


# ---------------------------------------------------------------------------
# Episode edit dialog
# ---------------------------------------------------------------------------

class EpisodeDialog(ctk.CTkToplevel):
    def __init__(self, parent: tk.Misc, item: BatchItem, on_save: Callable[[], None]) -> None:
        super().__init__(parent)
        self.title("Set episode")
        self.resizable(False, False)
        self.transient(parent.winfo_toplevel())
        self._item, self._on_save = item, on_save

        ctk.CTkLabel(self, text=os.path.basename(item.path), wraplength=380,
                     font=ctk.CTkFont(weight="bold")).pack(padx=20, pady=(16, 10))

        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(padx=20, pady=(0, 6))
        self._season = tk.StringVar(value="" if item.season is None else str(item.season))
        self._episode = tk.StringVar(value="" if item.episode is None else str(item.episode))
        self._end = tk.StringVar(value="" if item.episode_end is None else str(item.episode_end))
        for label, var in (("Season", self._season), ("Episode", self._episode),
                           ("to (optional)", self._end)):
            ctk.CTkLabel(row, text=label).pack(side="left", padx=(8, 4))
            entry = ctk.CTkEntry(row, textvariable=var, width=52)
            entry.pack(side="left")
            if var is self._season:
                entry.focus_set()

        ctk.CTkLabel(self, text="Use \"to\" for files that contain two episodes.",
                     text_color="gray60").pack(padx=20)
        self._err = ctk.CTkLabel(self, text="", text_color=_RED)
        self._err.pack(padx=20)

        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.pack(pady=(4, 16))
        ctk.CTkButton(btns, text="Save", width=90, command=self._save).pack(side="left", padx=6)
        ctk.CTkButton(btns, text="Cancel", width=90, fg_color="gray40", hover_color="gray30",
                      command=self.destroy).pack(side="left", padx=6)
        self.bind("<Return>", lambda _e: self._save())
        self.bind("<Escape>", lambda _e: self.destroy())
        self.after(10, self._raise)

    def _raise(self) -> None:
        self.lift()
        self.focus_force()
        try:
            self.grab_set()
        except tk.TclError:
            pass

    def _save(self) -> None:
        try:
            season = int(self._season.get())
            episode = int(self._episode.get())
            end = int(self._end.get()) if self._end.get().strip() else None
        except ValueError:
            self._err.configure(text="Season and episode must be numbers.")
            return
        if season < 0 or episode < 0 or (end is not None and end <= episode):
            self._err.configure(text="Check the numbers (\"to\" must be after the episode).")
            return
        it = self._item
        it.season, it.episode, it.episode_end, it.confirmed = season, episode, end, True
        self.destroy()
        self._on_save()


# ---------------------------------------------------------------------------
# Summary dialog
# ---------------------------------------------------------------------------

class SummaryDialog(ctk.CTkToplevel):
    def __init__(self, parent: tk.Misc, result: BatchResult) -> None:
        super().__init__(parent)
        self.title("Batch summary")
        self.transient(parent.winfo_toplevel())
        c = result.counts
        head = "Batch cancelled" if result.cancelled else "Batch finished"
        ctk.CTkLabel(self, text=head, font=ctk.CTkFont(size=16, weight="bold")).pack(
            padx=20, pady=(16, 2), anchor="w")
        line = f"Converted {c['converted']}   ·   Skipped {c['skipped']}   ·   Failed {c['failed']}"
        if c.get("not_run"):
            line += f"   ·   Not run {c['not_run']}"
        ctk.CTkLabel(self, text=line).pack(padx=20, anchor="w")

        box = ctk.CTkTextbox(self, width=680, height=340, wrap="none",
                             font=ctk.CTkFont(family="Consolas", size=12))
        box.pack(padx=20, pady=10, fill="both", expand=True)
        box.insert("1.0", result.summary_text)
        box.configure(state="disabled")

        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.pack(pady=(0, 16))
        ctk.CTkButton(btns, text="Open summary file", width=140,
                      command=lambda: os.startfile(result.summary_path)).pack(side="left", padx=6)
        ctk.CTkButton(btns, text="Open log", width=100,
                      command=lambda: os.startfile(result.log_path)).pack(side="left", padx=6)
        ctk.CTkButton(btns, text="Close", width=90, fg_color="gray40", hover_color="gray30",
                      command=self.destroy).pack(side="left", padx=6)
        self.after(10, self.lift)


# ---------------------------------------------------------------------------
# Batch panel
# ---------------------------------------------------------------------------

class BatchView(ctk.CTkFrame):
    def __init__(self, master: tk.Misc, app: "App", on_close: Callable[[], None]) -> None:
        super().__init__(master, fg_color="transparent")
        self._app = app
        self._on_close = on_close

        self.items: list[BatchItem] = []
        self._plans: dict[int, Plan] = {}
        self._iid: dict[int, BatchItem] = {}
        self._runner: Optional[BatchRunner] = None
        self._last_result: Optional[BatchResult] = None
        self._rates = _rates_from_cfg(app._cfg)
        self._probing = False
        self._benchmarking = False
        self._refresh_job: Optional[str] = None
        self._title_touched = False   # user typed a show title; don't overwrite it
        self._closed = False

        self._build()
        self._update_hint()
        self._apply_tree_style()

    # ------------------------------------------------------------------ layout

    def _build(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        # --- header ---
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        self._head_label = ctk.CTkLabel(head, text="Batch", font=ctk.CTkFont(size=15, weight="bold"))
        self._head_label.pack(side="left")
        self._close_btn = ctk.CTkButton(
            head, text="Close batch", width=100, height=28, fg_color="gray40",
            hover_color="gray30", command=self._close)
        self._close_btn.pack(side="right")
        self._add_folder_btn = ctk.CTkButton(head, text="Add folder…", width=100, height=28,
                                             command=self._add_folder)
        self._add_folder_btn.pack(side="right", padx=6)
        self._add_files_btn = ctk.CTkButton(head, text="Add files…", width=90, height=28,
                                            command=self._add_files)
        self._add_files_btn.pack(side="right")

        # --- batch-wide metadata ---
        meta = ctk.CTkFrame(self)
        meta.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        meta.columnconfigure(5, weight=1)
        self._title_var = tk.StringVar()
        self._year_var = tk.StringVar()
        self._output_var = tk.StringVar(value=self._app._cfg.get("output_dir", ""))
        ctk.CTkLabel(meta, text="Show title:").grid(row=0, column=0, padx=(12, 6), pady=8)
        self._title_entry = ctk.CTkEntry(meta, textvariable=self._title_var, width=260)
        self._title_entry.grid(row=0, column=1, pady=8)
        ctk.CTkLabel(meta, text="Year:").grid(row=0, column=2, padx=(14, 6))
        self._year_entry = ctk.CTkEntry(meta, textvariable=self._year_var, width=64)
        self._year_entry.grid(row=0, column=3)
        ctk.CTkLabel(meta, text="Output:").grid(row=0, column=4, padx=(14, 6))
        self._output_entry = ctk.CTkEntry(meta, textvariable=self._output_var)
        self._output_entry.grid(row=0, column=5, sticky="ew")
        self._output_btn = ctk.CTkButton(meta, text="Browse", width=70, command=self._browse_output)
        self._output_btn.grid(row=0, column=6, padx=(6, 12))
        self._keep_names = tk.BooleanVar(value=False)
        self._keep_chk = ctk.CTkCheckBox(
            meta, text="Keep original filenames (no episode parsing, all files in one folder)",
            variable=self._keep_names, command=self._on_keep_names_toggle)
        self._keep_chk.grid(row=1, column=0, columnspan=7, padx=12, pady=(0, 4), sticky="w")
        self._hint = ctk.CTkLabel(meta, text="", text_color="gray60", font=ctk.CTkFont(size=11))
        self._hint.grid(row=2, column=0, columnspan=7, padx=12, pady=(0, 8), sticky="w")
        self._title_entry.bind("<Key>", lambda _e: setattr(self, "_title_touched", True))
        for var in (self._title_var, self._year_var, self._output_var):
            var.trace_add("write", lambda *_: self._schedule_refresh())

        # --- queue list ---
        list_frame = tk.Frame(self, bd=0, highlightthickness=0)
        list_frame.grid(row=2, column=0, sticky="nsew")
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)
        cols = ("file", "episode", "video", "action", "status", "notes")
        self._tree = ttk.Treeview(list_frame, columns=cols, show="headings",
                                  style="Batch.Treeview", selectmode="extended")
        for col, text, width, stretch in (
            ("file", "File", 300, True), ("episode", "Episode", 80, False),
            ("video", "Video", 120, False), ("action", "Action", 80, False),
            ("status", "Status", 90, False), ("notes", "Notes", 300, True),
        ):
            self._tree.heading(col, text=text, anchor="w")
            self._tree.column(col, width=width, minwidth=50, stretch=stretch, anchor="w")
        sb = ttk.Scrollbar(list_frame, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=sb.set)
        self._tree.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")
        self._tree.bind("<Double-1>", self._on_double_click)
        self._tree.bind("<Delete>", lambda _e: self._remove_selected())

        for w in (self, self._tree):
            w.drop_target_register(tkinterdnd2.DND_FILES)
            w.dnd_bind("<<Drop>>", self._on_drop)

        # --- list actions + estimate ---
        actions = ctk.CTkFrame(self, fg_color="transparent")
        actions.grid(row=3, column=0, sticky="ew", pady=(6, 0))
        self._edit_btn = ctk.CTkButton(actions, text="Set episode…", width=110, height=28,
                                       command=self._edit_selected)
        self._edit_btn.pack(side="left")
        self._remove_btn = ctk.CTkButton(actions, text="Remove selected", width=130, height=28,
                                         fg_color="gray40", hover_color="gray30",
                                         command=self._remove_selected)
        self._remove_btn.pack(side="left", padx=6)
        self._issues_only = tk.BooleanVar(value=False)
        self._issues_chk = ctk.CTkCheckBox(actions, text="Show only files that need attention",
                                           variable=self._issues_only, command=self._rebuild_tree)
        self._issues_chk.pack(side="left", padx=10)

        self._estimate_label = ctk.CTkLabel(self, text="", anchor="w", justify="left")
        self._estimate_label.grid(row=4, column=0, sticky="ew", pady=(6, 0))

        # --- progress ---
        prog = ctk.CTkFrame(self, fg_color="transparent")
        prog.grid(row=5, column=0, sticky="ew", pady=(6, 0))
        prog.columnconfigure(1, weight=1)
        ctk.CTkLabel(prog, text="Overall", width=70, anchor="w").grid(row=0, column=0)
        self._overall_bar = ctk.CTkProgressBar(prog)
        self._overall_bar.grid(row=0, column=1, sticky="ew", padx=6)
        self._overall_bar.set(0)
        self._overall_label = ctk.CTkLabel(prog, text="", anchor="w", width=300)
        self._overall_label.grid(row=0, column=2, sticky="w")
        ctk.CTkLabel(prog, text="Current", width=70, anchor="w").grid(row=1, column=0)
        self._current_bar = ctk.CTkProgressBar(prog)
        self._current_bar.grid(row=1, column=1, sticky="ew", padx=6)
        self._current_bar.set(0)
        self._current_label = ctk.CTkLabel(prog, text="", anchor="w", width=300)
        self._current_label.grid(row=1, column=2, sticky="w")

        # --- run controls ---
        ctl = ctk.CTkFrame(self, fg_color="transparent")
        ctl.grid(row=6, column=0, pady=(10, 0))
        self._start_btn = ctk.CTkButton(ctl, text="Start batch", width=180, height=40,
                                        font=ctk.CTkFont(size=14, weight="bold"),
                                        command=self._start)
        self._start_btn.pack(side="left", padx=6)
        self._pause_btn = ctk.CTkButton(ctl, text="Pause", width=100, height=40,
                                        command=self._toggle_pause, state="disabled")
        self._pause_btn.pack(side="left", padx=6)
        self._cancel_btn = ctk.CTkButton(ctl, text="Cancel", width=100, height=40,
                                         fg_color="gray40", hover_color="gray30",
                                         command=self._cancel, state="disabled")
        self._cancel_btn.pack(side="left", padx=6)
        self._summary_btn = ctk.CTkButton(ctl, text="Show summary", width=120, height=40,
                                          fg_color="gray40", hover_color="gray30",
                                          command=self._show_summary)
        self._logs_btn = ctk.CTkButton(ctl, text="Open log folder", width=120, height=40,
                                       fg_color="gray40", hover_color="gray30",
                                       command=self._open_logs)
        self._logs_btn.pack(side="left", padx=6)

    def _apply_tree_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        dark = ctk.get_appearance_mode() == "Dark"
        bg, fg, head_bg = ("#2b2b2b", "#dce4ee", "#3a3a3a") if dark else ("#ffffff", "#1a1a1a", "#e2e2e2")
        style.configure("Batch.Treeview", background=bg, fieldbackground=bg, foreground=fg,
                        rowheight=24, borderwidth=0, font=("Segoe UI", 10))
        style.configure("Batch.Treeview.Heading", background=head_bg, foreground=fg,
                        relief="flat", font=("Segoe UI", 10, "bold"))
        style.map("Batch.Treeview", background=[("selected", "#1f6aa5")],
                  foreground=[("selected", "#ffffff")])
        self._tree.tag_configure("attention", foreground=_ORANGE)
        self._tree.tag_configure("failed", foreground="#e74c3c")
        self._tree.tag_configure("done", foreground="#4caf50")
        self._tree.tag_configure("skipped", foreground="gray55")
        self._tree.tag_configure("converting", foreground="#5aa9e6")

    # ------------------------------------------------------------------ input

    @property
    def running(self) -> bool:
        return self._runner is not None

    def add_paths(self, paths: list[str]) -> int:
        if self.running:
            return 0
        known = {os.path.normcase(i.path) for i in self.items}
        new = [BatchItem.from_path(p, r) for p, r in batch.collect_videos(paths)
               if os.path.normcase(p) not in known]
        if not new:
            return 0
        self.items.extend(new)
        if not self._title_touched:
            self._title_var.set(batch.most_common([i.guess.title for i in self.items]))
            self._year_var.set(batch.most_common([i.guess.year for i in self.items]))
        self._rebuild_tree()
        self._start_probing()
        return len(new)

    def _on_drop(self, event: tk.Event) -> None:
        paths = list(self.tk.splitlist(event.data))
        if self.running:
            return
        if not self.add_paths(paths):
            messagebox.showinfo("Nothing added", "No new video files found in what was dropped.",
                                parent=self)

    def _add_files(self) -> None:
        exts = " ".join(f"*{e}" for e in batch.VIDEO_EXTENSIONS)
        paths = filedialog.askopenfilenames(parent=self, title="Add video files",
                                            filetypes=[("Video files", exts), ("All files", "*.*")])
        if paths:
            self.add_paths(list(paths))

    def _add_folder(self) -> None:
        d = filedialog.askdirectory(parent=self, title="Add a folder (subfolders included)")
        if d and not self.add_paths([d]):
            messagebox.showinfo("Nothing added", "No new video files found in that folder.",
                                parent=self)

    def _browse_output(self) -> None:
        d = filedialog.askdirectory(parent=self, title="Select output folder")
        if d:
            self._output_var.set(os.path.normpath(d))

    # ------------------------------------------------------------------ probing

    def _post(self, fn: Callable[[], None]) -> None:
        """Run `fn` on the UI thread; safe to call from workers after the view is closed."""
        if self._closed:
            return
        try:
            self.after(0, fn)
        except (RuntimeError, tk.TclError):
            self._closed = True

    def destroy(self) -> None:
        self._closed = True
        super().destroy()

    def _start_probing(self) -> None:
        if self._probing:
            return
        self._probing = True
        ffprobe = self._app._ffprobe_exe

        def _work() -> None:
            while not self._closed:
                pending = [i for i in list(self.items) if i.info is None and not i.probe_error]
                if not pending:
                    break
                it = pending[0]
                try:
                    it.info = probe(it.path, ffprobe_exe=ffprobe)
                except Exception as exc:
                    it.probe_error = batch.short_reason(str(exc))
                self._post(self._schedule_refresh)
            self._post(self._on_probing_done)

        threading.Thread(target=_work, daemon=True).start()

    def _on_probing_done(self) -> None:
        self._probing = False
        self._refresh()
        if self._rates.source == "default" and not self._benchmarking:
            sample = next((i for i in self.items if i.info and i.info.needs_reencode), None)
            if sample:
                self._run_benchmark(sample)

    def _run_benchmark(self, item: BatchItem) -> None:
        self._benchmarking = True
        self._refresh()
        ffmpeg = self._app._ffmpeg_exe

        def _work() -> None:
            px = batch.benchmark_encode(item.path, item.info, ffmpeg)
            self._post(lambda: self._on_benchmark(px))

        threading.Thread(target=_work, daemon=True).start()

    def _on_benchmark(self, px: Optional[float]) -> None:
        self._benchmarking = False
        if px:
            self._rates.encode_px_per_sec = px
            self._rates.source = "test encode"
            self._save_rates(self._rates)
        self._refresh()

    def _save_rates(self, rates: Rates) -> None:
        self._app._cfg["batch_rates"] = {
            "encode_px_per_sec": rates.encode_px_per_sec,
            "remux_bytes_per_sec": rates.remux_bytes_per_sec,
            "source": rates.source,
        }
        config.save(self._app._cfg)

    # ------------------------------------------------------------------ list

    def _show(self) -> str:
        return self._title_var.get().strip()

    def _year(self) -> Optional[str]:
        return self._year_var.get().strip() or None

    def _out(self) -> str:
        return self._output_var.get().strip()

    def _keep(self) -> bool:
        return self._keep_names.get()

    def _plan(self) -> dict:
        return batch.plan_items(self.items, self._show(), self._year(), self._out(), self._keep())

    def _update_hint(self) -> None:
        if self._keep():
            text = ("Each file keeps its own name, with the extension changed to .mkv, "
                    "and goes straight into the output folder.")
        else:
            text = ("Title and year apply to every file. Season and episode come from "
                    "each filename — double-click a row to fix one.")
        self._hint.configure(text=text)
        state = "disabled" if self._keep() else "normal"
        for w in (self._title_entry, self._year_entry, self._edit_btn):
            w.configure(state=state)

    def _on_keep_names_toggle(self) -> None:
        self._update_hint()
        self._rebuild_tree()

    def _schedule_refresh(self) -> None:
        if self._refresh_job:
            self.after_cancel(self._refresh_job)
        self._refresh_job = self.after(250, self._refresh)

    def _refresh(self) -> None:
        self._refresh_job = None
        if not self.running:
            self._plans = self._plan()
        for iid, it in self._iid.items():
            if self._tree.exists(iid):
                self._update_row(iid, it)
        if self._issues_only.get():
            self._rebuild_tree()
        self._update_summary()

    def _rebuild_tree(self) -> None:
        if not self.running:
            self._plans = self._plan()
        self._tree.delete(*self._tree.get_children())
        self._iid.clear()
        for it in self.items:
            if self._issues_only.get() and not self._needs_attention(it):
                continue
            iid = str(id(it))
            self._iid[iid] = it
            self._tree.insert("", "end", iid=iid)
            self._update_row(iid, it)
        self._update_summary()

    def _needs_attention(self, it: BatchItem) -> bool:
        if it.status == FAILED:
            return True
        plan = self._plans.get(id(it))
        return bool(plan and plan.needs_attention and it.status == QUEUED)

    def _update_row(self, iid: str, it: BatchItem) -> None:
        plan = self._plans.get(id(it)) or Plan("probing")
        info = it.info
        video = (f"{info.codec} {info.width}x{info.height}" if info
                 else "unreadable" if it.probe_error else "…")
        action = _ACTION_TEXT.get(plan.action, "")
        if info and it.status in (CONVERTING, DONE):
            action = "Re-encode" if info.needs_reencode else "Remux"

        tag = ""
        if it.status == QUEUED:
            if plan.needs_attention:
                status, tag = "Needs fix" if plan.action != "fail" else "Will fail", "attention"
                if plan.action in ("reencode", "remux"):
                    status = "Check"
            elif plan.action == "skip":
                status, tag = "Will skip", "skipped"
            elif plan.action == "probing":
                status = "Inspecting"
            else:
                status = "Queued"
            notes = plan.note
        elif it.status == CONVERTING:
            status, tag = "Converting", "converting"
            notes = it.reason
        else:
            status = it.status.capitalize()
            tag = {DONE: "done", SKIPPED: "skipped", FAILED: "failed"}[it.status]
            notes = it.reason

        episode = "—" if self._keep() else (it.tag or "?")
        self._tree.item(iid, values=(it.display_name, episode, video, action, status, notes),
                        tags=(tag,) if tag else ())

    def _update_summary(self) -> None:
        n = len(self.items)
        self._head_label.configure(text=f"Batch — {n} file{'s' if n != 1 else ''}")
        if self.running:
            return
        if any(i.status != QUEUED for i in self.items):
            # Showing results of a finished run; "Run again" re-plans everything.
            self._start_btn.configure(state="normal")
            return
        if self._probing:
            done = sum(1 for i in self.items if i.info or i.probe_error)
            self._estimate_label.configure(text=f"Inspecting files… {done} of {n}",
                                           text_color="gray60")
            self._start_btn.configure(state="disabled")
            return

        plans = [self._plans.get(id(i)) for i in self.items if i.status == QUEUED]
        reenc = [i for i in self.items if self._plan_is(i, "reencode")]
        remux = [i for i in self.items if self._plan_is(i, "remux")]
        exists = sum(1 for p in plans if p and p.action == "skip" and p.note == "Already converted")
        attention = sum(1 for p in plans if p and p.needs_attention)

        parts = [f"{len(reenc)} to re-encode", f"{len(remux)} to remux"]
        if exists:
            parts.append(f"{exists} already converted")
        if attention:
            parts.append(f"{attention} need attention")
        text = " · ".join(parts)

        if self._benchmarking:
            text += "\nMeasuring this computer's encoding speed for the time estimate…"
        elif reenc or remux:
            secs = sum(batch.estimate_item(i.info, self._rates) for i in reenc + remux)
            basis = {"test encode": "based on a short test encode",
                     "previous conversions": "based on your previous conversions",
                     "default": "rough guess"}.get(self._rates.source, "")
            text += f"\nEstimated time: about {batch.format_duration(secs)} ({basis})"
        self._estimate_label.configure(text=text, text_color=_ORANGE if attention else ("gray10", "gray90"))
        self._start_btn.configure(state="normal" if (reenc or remux) else "disabled")

    def _plan_is(self, it: BatchItem, action: str) -> bool:
        p = self._plans.get(id(it))
        return it.status == QUEUED and p is not None and p.action == action

    def _selected_items(self) -> list[BatchItem]:
        return [self._iid[iid] for iid in self._tree.selection() if iid in self._iid]

    def _on_double_click(self, event: tk.Event) -> None:
        iid = self._tree.identify_row(event.y)
        if iid and iid in self._iid and not self.running and not self._keep():
            EpisodeDialog(self, self._iid[iid], self._rebuild_tree)

    def _edit_selected(self) -> None:
        sel = self._selected_items()
        if self.running or not sel or self._keep():
            return
        EpisodeDialog(self, sel[0], self._rebuild_tree)

    def _remove_selected(self) -> None:
        if self.running:
            return
        sel = {id(i) for i in self._selected_items()}
        if not sel:
            return
        self.items = [i for i in self.items if id(i) not in sel]
        self._rebuild_tree()

    # ------------------------------------------------------------------ run

    def _set_editing(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for w in (self._title_entry, self._year_entry, self._output_entry, self._output_btn,
                  self._add_files_btn, self._add_folder_btn, self._edit_btn, self._remove_btn,
                  self._close_btn, self._start_btn, self._keep_chk):
            w.configure(state=state)
        if enabled:
            self._update_hint()
        self._pause_btn.configure(state="disabled" if enabled else "normal")
        self._cancel_btn.configure(state="disabled" if enabled else "normal")

    def _start(self) -> None:
        app = self._app
        if not (app._ffmpeg_exe and app._ffprobe_exe):
            app._open_setup()
            return
        show, year, out, keep = self._show(), self._year(), self._out(), self._keep()
        if not show and not keep:
            messagebox.showwarning("Missing title", "Please enter the show title.", parent=self)
            return
        if year and not keep and not re.fullmatch(r"(19|20)\d{2}", year):
            messagebox.showwarning("Check year", "The year should be four digits, e.g. 2004.",
                                   parent=self)
            return
        if not out:
            messagebox.showwarning("Missing output", "Please choose an output folder.", parent=self)
            return

        # A previous run in this session: re-queue everything; finished files
        # will be detected as already converted.
        for it in self.items:
            it.status, it.reason = QUEUED, ""
        self._plans = batch.plan_items(self.items, show, year, out, keep)

        unfixed = [i for i in self.items if self._plans[id(i)].needs_attention
                   and self._plans[id(i)].action in ("skip", "fail")]
        guessed = [i for i in self.items if self._plans[id(i)].needs_attention
                   and self._plans[id(i)].action in ("reencode", "remux")]
        if unfixed or guessed:
            msg = []
            if unfixed:
                msg.append(f"{len(unfixed)} file(s) need attention and will be skipped or fail.")
            if guessed:
                msg.append(f"{len(guessed)} file(s) have guessed episode numbers you haven't checked.")
            msg.append("\nTick \"Show only files that need attention\" to review them.\n\nStart anyway?")
            if not messagebox.askyesno("Some files need attention", "\n".join(msg), parent=self):
                return

        app._cfg["output_dir"] = out
        config.save(app._cfg)

        def ui(fn):
            return lambda *a: self._post(lambda: fn(*a))

        self._todo = sum(1 for i in self.items if self._plans[id(i)].action in ("reencode", "remux"))
        self._finished = 0
        self._runner = BatchRunner(
            self.items, show, year, out, app._ffmpeg_exe, app._ffprobe_exe,
            str(config.LOG_DIR), self._rates,
            on_item=ui(self._on_item), on_progress=ui(self._on_progress),
            on_notice=ui(self._on_notice), on_rates=ui(self._save_rates),
            on_finished=ui(self._on_finished), keep_names=keep,
        )
        self._set_editing(False)
        self._summary_btn.pack_forget()
        self._pause_btn.configure(text="Pause")
        self._overall_bar.set(0)
        self._current_bar.set(0)
        self._overall_label.configure(text="Starting…")
        self._current_label.configure(text="")
        self._estimate_label.configure(text=f"Log: {self._runner.log_path}", text_color="gray60")
        app._last_cmd = None
        self._runner.start()

    def _on_item(self, it: BatchItem) -> None:
        iid = str(id(it))
        if self._tree.exists(iid):
            self._update_row(iid, it)
            if it.status == CONVERTING:
                self._tree.see(iid)
        if it.status == CONVERTING:
            self._current_bar.set(0)
            self._current_label.configure(text=f"{it.tag} · {it.reason} · 0%")
        elif it.status in (DONE, SKIPPED, FAILED) and self._runner and \
                self._plans.get(id(it), Plan("")).action in ("reencode", "remux"):
            self._finished += 1
        self._update_overall()

    def _on_progress(self, it: BatchItem, frac: float) -> None:
        if not self._runner:
            return
        self._current_bar.set(frac)
        self._current_label.configure(text=f"{it.tag} · {it.reason} · {int(frac * 100)}%")
        iid = str(id(it))
        if self._tree.exists(iid):
            self._tree.set(iid, "notes", f"{it.reason} · {int(frac * 100)}%")
        self._update_overall(frac)

    def _update_overall(self, frac: float = 0.0) -> None:
        r = self._runner
        if not r or not self._todo:
            return
        done = min(self._finished + (frac if r.current else 0), self._todo)
        self._overall_bar.set(done / self._todo)
        pos = min(self._finished + 1, self._todo)
        text = f"File {pos} of {self._todo}"
        if r.paused:
            text += " · paused"
        else:
            text += f" · about {batch.format_duration(r.remaining_seconds())} left"
        self._overall_label.configure(text=text)

    def _on_notice(self, msg: str) -> None:
        self._pause_btn.configure(text="Resume")
        self._update_overall()
        messagebox.showwarning("Batch paused", msg, parent=self)

    def _toggle_pause(self) -> None:
        r = self._runner
        if not r:
            return
        if r.paused:
            r.resume()
            self._pause_btn.configure(text="Pause")
        else:
            r.pause()
            self._pause_btn.configure(text="Resume")
        self._update_overall()

    def _cancel(self) -> None:
        if not self._runner:
            return
        if messagebox.askyesno(
            "Cancel batch?",
            "Stop the batch now?\n\nThe file being converted is discarded; finished files "
            "are kept. Running the same folder again picks up where it left off.",
            parent=self,
        ):
            self._cancel_btn.configure(state="disabled")
            self._overall_label.configure(text="Cancelling…")
            self._runner.cancel()

    def cancel_and_wait(self, timeout: float = 15.0) -> None:
        """Used when the app window is closed mid-batch."""
        if self._runner:
            self._runner.cancel()
            self._runner.join(timeout)

    def _on_finished(self, result: BatchResult) -> None:
        self._runner = None
        self._last_result = result
        self._set_editing(True)
        self._pause_btn.configure(text="Pause")
        c = result.counts
        self._overall_label.configure(
            text=("Cancelled · " if result.cancelled else "Finished · ") +
                 f"{c['converted']} converted, {c['skipped']} skipped, {c['failed']} failed")
        if not result.cancelled:
            self._overall_bar.set(1.0)
        self._current_bar.set(0)
        self._current_label.configure(text="")
        self._start_btn.configure(text="Run again")
        self._summary_btn.pack(side="left", padx=6, before=self._logs_btn)
        for iid, it in self._iid.items():
            if self._tree.exists(iid):
                self._update_row(iid, it)
        self._estimate_label.configure(text=f"Summary: {result.summary_path}",
                                       text_color="gray60")
        self._show_summary()

    def _show_summary(self) -> None:
        if self._last_result:
            SummaryDialog(self, self._last_result)

    def _open_logs(self) -> None:
        config.LOG_DIR.mkdir(parents=True, exist_ok=True)
        os.startfile(str(config.LOG_DIR))

    def _close(self) -> None:
        if self.running:
            return
        self._on_close()

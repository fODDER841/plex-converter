"""
Rename TV episodes converted by older versions of Plex Converter to the
current naming, so batch conversion recognises them as already done.

    Old:  Show (2004)\\Season 01\\Show - S01E05.mkv
    New:  Show (2004)\\Season 01\\Show (2004) - s01e05.mkv

Usage (dry run first, then apply):

    python scripts\\rename_old_tv_names.py "D:\\TV"
    python scripts\\rename_old_tv_names.py "D:\\TV" --apply

The folder can be a whole library or a single show. Only files exactly
matching the old pattern, inside a "Season NN" folder whose show folder
matches the file's title, are touched. With --apply, a CSV of every rename
(old path, new path) is written to %APPDATA%\\PlexConverter\\logs for undo.
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import LOG_DIR  # noqa: E402
from src.naming import episode_tag  # noqa: E402

# Exactly what the old version wrote: "{title} - S{season:02d}E{episode:02d}.mkv"
_OLD = re.compile(r"^(?P<show>.+) - S(?P<s>\d{2,})E(?P<e>\d{2,})\.mkv$")
_SEASON_DIR = re.compile(r"^Season \d{2,}$")


@dataclass
class Rename:
    old: Path
    new: Path
    ok: bool
    reason: str = ""


def plan_renames(root: str | Path) -> list[Rename]:
    plans: list[Rename] = []
    for dirpath, _dirs, files in os.walk(root):
        folder = Path(dirpath)
        if not _SEASON_DIR.match(folder.name):
            continue
        show_folder = folder.parent.name
        for name in sorted(files):
            m = _OLD.match(name)
            if not m:
                continue
            old = folder / name
            show = m.group("show")
            if show_folder != show and not re.fullmatch(
                    re.escape(show) + r" \((?:19|20)\d{2}\)", show_folder):
                plans.append(Rename(old, old, False,
                                    f"title doesn't match its show folder '{show_folder}'"))
                continue
            new = folder / f"{show_folder} - {episode_tag(int(m.group('s')), int(m.group('e')))}.mkv"
            same_file = os.path.normcase(str(old)) == os.path.normcase(str(new))
            if new.exists() and not same_file:
                plans.append(Rename(old, new, False, "a file with the new name already exists"))
            else:
                plans.append(Rename(old, new, True))
    return plans


def apply_renames(plans: list[Rename]) -> list[Rename]:
    done: list[Rename] = []
    for r in plans:
        if not r.ok:
            continue
        try:
            os.rename(r.old, r.new)  # also handles case-only renames on Windows
            done.append(r)
        except OSError as exc:
            r.ok, r.reason = False, str(exc)
    return done


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("folder", help="TV library folder (or a single show folder)")
    ap.add_argument("--apply", action="store_true", help="actually rename (default: dry run)")
    args = ap.parse_args(argv)

    if not os.path.isdir(args.folder):
        print(f"Not a folder: {args.folder}")
        return 2

    plans = plan_renames(args.folder)
    todo = [r for r in plans if r.ok]
    skipped = [r for r in plans if not r.ok]
    if not plans:
        print("No files in the old naming format were found.")
        return 0

    for r in todo:
        print(f"{'RENAME' if args.apply else 'would rename'}  {r.old}\n            -> {r.new.name}")
    for r in skipped:
        print(f"SKIP  {r.old}\n            ({r.reason})")

    if not args.apply:
        print(f"\nDry run: {len(todo)} file(s) would be renamed, {len(skipped)} skipped. "
              "Re-run with --apply to rename.")
        return 0

    done = apply_renames(todo)
    failed = [r for r in todo if not r.ok]
    for r in failed:
        print(f"FAILED  {r.old}\n            ({r.reason})")
    if done:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        undo = LOG_DIR / f"renamed_{datetime.now():%Y-%m-%d_%H%M%S}.csv"
        with open(undo, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["old_path", "new_path"])
            w.writerows([str(r.old), str(r.new)] for r in done)
        print(f"\nList of renames (for undo): {undo}")
    print(f"\nRenamed {len(done)}, skipped {len(skipped)}, failed {len(failed)}.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

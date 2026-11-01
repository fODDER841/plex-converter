import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Common release-group noise to strip from titles.
_NOISE = re.compile(
    r"\b(?:blu[_\-]?ray|bdrip|brrip|dvdrip|dvdscr|dvd|webrip|web[_\-]?dl|"
    r"hdtv|pdtv|hdrip|1080[pi]|720[pi]|480[pi]|576[pi]|4k|uhd|hdr10?|sdr|"
    r"hevc|x265|x264|h265|h264|avc|xvid|divx|vc1|"
    r"aac|ac3|dts|truehd|flac|mp3|dd[_\.]?5[_\.]1|atmos|"
    r"extended|theatrical|directors?[_\s]?cut|remastered|uncut|"
    r"proper|rerip|multi|dual[_\s]?audio|"
    r"yts|yify|rarbg|eztv|ettv|ctrlhd|fgt|ntg|ion10|sparks)\b",
    re.IGNORECASE,
)

# SxxExx or sxex (must appear after at least one word).
_TV = re.compile(
    r"^(?P<show>.+?)[._\s\-]+[Ss](?P<season>\d{1,2})[Ee](?P<episode>\d{1,2})",
    re.IGNORECASE,
)

# Title followed by a 4-digit year in round brackets or as a bare token.
_MOVIE_YEAR = re.compile(
    r"^(?P<title>.+?)[._\s(]+(?P<year>(?:19|20)\d{2})[._\s)]*",
)

# Standalone year anywhere in the show name (for TV files that embed it).
_YEAR_IN_NAME = re.compile(r"\b((?:19|20)\d{2})\b")


@dataclass
class ParsedName:
    is_tv: bool
    title: str
    year: Optional[str] = None
    season: Optional[int] = None
    episode: Optional[int] = None


def _clean(raw: str) -> str:
    s = raw.replace(".", " ").replace("_", " ").replace("-", " ")
    s = _NOISE.sub(" ", s)
    # Strip trailing/leading junk and title-case.
    return " ".join(s.split()).strip().title()


def parse_filename(path: str) -> ParsedName:
    stem = Path(path).stem

    tv_match = _TV.match(stem)
    if tv_match:
        show_raw = tv_match.group("show")
        year_m = _YEAR_IN_NAME.search(show_raw)
        year = year_m.group(1) if year_m else None
        if year_m:
            show_raw = show_raw[: year_m.start()] + show_raw[year_m.end() :]
        return ParsedName(
            is_tv=True,
            title=_clean(show_raw),
            year=year,
            season=int(tv_match.group("season")),
            episode=int(tv_match.group("episode")),
        )

    movie_match = _MOVIE_YEAR.match(stem)
    if movie_match:
        return ParsedName(
            is_tv=False,
            title=_clean(movie_match.group("title")),
            year=movie_match.group("year"),
        )

    # Fallback: no year found — let the user fill it in.
    return ParsedName(is_tv=False, title=_clean(stem), year=None)


# ---------------------------------------------------------------------------
# Episode parsing for batch mode (uses folder names as well as the filename)
# ---------------------------------------------------------------------------

# S01E02, s1e2, S01.E02, S01E01E02, S01E01-E02, S01E01-02
_SXXEYY = re.compile(
    r"(?<![a-z0-9])s(?P<s>\d{1,2})[ ._-]?e(?P<e>\d{1,3})"
    r"(?:(?:[ ._]?-[ ._]?e?|e)(?P<e2>\d{1,3}))?(?![0-9a-z])",
    re.IGNORECASE,
)
# 1x02, 01x02, 1x02-1x03
_NXNN = re.compile(
    r"(?<![0-9a-z])(?P<s>\d{1,2})x(?P<e>\d{2,3})(?:-(?:\d{1,2}x)?(?P<e2>\d{2,3}))?(?![0-9])",
    re.IGNORECASE,
)
# "Season 1", "Series 02", "Season.03"
_SEASON_WORD = re.compile(r"(?<![a-z])(?:season|series)[ ._-]*(?P<s>\d{1,2})(?!\d)", re.IGNORECASE)
# Folder that is just "S01" / "S1"
_SEASON_SHORT_DIR = re.compile(r"^s(?P<s>\d{1,2})$", re.IGNORECASE)
# "Episode 5", "Ep05", "E05"
_EPISODE_WORD = re.compile(
    r"(?<![a-z0-9])(?:episode|ep|e)[ ._-]*(?P<e>\d{1,3})(?!\d)", re.IGNORECASE
)
# Leading number: "05 - Title", "105 Title"
_LEADING_NUM = re.compile(r"^(?P<n>\d{1,4})(?=$|[ ._\-])")
# Number after the title: "Show 105", "Show - 1012 - Title" (not a year)
_INNER_NUM = re.compile(r"[ ._\-](?P<n>\d{3,4})(?=$|[ ._\-])")


@dataclass
class EpisodeGuess:
    title: str
    year: Optional[str]
    season: Optional[int]
    episode: Optional[int]
    episode_end: Optional[int] = None
    # True when the numbers came from an ambiguous pattern (e.g. "102 - Pilot")
    # and should be checked by the user.
    uncertain: bool = False


def _split_year(raw: str) -> tuple[str, Optional[str]]:
    m = _YEAR_IN_NAME.search(raw)
    if not m:
        return raw, None
    return raw[: m.start()] + raw[m.end():], m.group(1)


def _show_from_dirs(dirs: list[str]) -> tuple[str, Optional[str]]:
    """Nearest ancestor folder that isn't a season folder."""
    for d in reversed(dirs):
        if _SEASON_WORD.search(d) or _SEASON_SHORT_DIR.match(d.strip()):
            continue
        raw, year = _split_year(d)
        title = _clean(re.sub(r"[()\[\]]", " ", raw))
        if title:
            return title, year
    return "", None


def _valid_end(start: int, end: Optional[str]) -> Optional[int]:
    if end is None:
        return None
    e2 = int(end)
    return e2 if start < e2 <= start + 9 else None


def parse_episode(path: str, root: Optional[str] = None) -> EpisodeGuess:
    """
    Guess show/season/episode from a file path. `root` is the folder the
    user dropped; folders between it and the file (and the root itself) are
    used as context for show title and season number.
    """
    p = Path(path)
    stem = p.stem
    if root:
        try:
            rel_dirs = list(p.parent.relative_to(root).parts)
            dirs = [Path(root).name] + rel_dirs
        except ValueError:
            dirs = [p.parent.name]
    else:
        dirs = [p.parent.name]
    dirs = [d for d in dirs if d]

    season = episode = episode_end = None
    uncertain = False
    title_part = ""

    m = _SXXEYY.search(stem) or _NXNN.search(stem)
    if m:
        season, episode = int(m.group("s")), int(m.group("e"))
        episode_end = _valid_end(episode, m.group("e2"))
        title_part = stem[: m.start()]
    else:
        # Season from the filename ("Season 2 Episode 5") or from a folder.
        sm = _SEASON_WORD.search(stem)
        if sm:
            season = int(sm.group("s"))
        else:
            for d in reversed(dirs):
                dm = _SEASON_WORD.search(d) or _SEASON_SHORT_DIR.match(d.strip())
                if dm:
                    season = int(dm.group("s"))
                    break

        em = _EPISODE_WORD.search(stem)
        lead = _LEADING_NUM.match(stem)
        if em:
            episode = int(em.group("e"))
            title_part = stem[: (sm.start() if sm and sm.start() < em.start() else em.start())]
        elif lead:
            n = lead.group("n")
            if season is not None and len(n) <= 3:
                episode = int(n)
                if len(n) == 3 and n.startswith(str(season)):  # "105" in Season 1
                    episode = int(n[len(str(season)):])
            elif season is None and len(n) in (3, 4):
                # "105" -> S1E05, "1012" -> S10E12. Plausible but ambiguous.
                season, episode = int(n[:-2]), int(n[-2:])
                uncertain = True
            elif season is None:
                episode = int(n)
                season = 1
                uncertain = True
        else:
            inner = next((m for m in _INNER_NUM.finditer(stem)
                          if not _YEAR_IN_NAME.fullmatch(m.group("n"))), None)
            if inner:
                n = inner.group("n")
                if season is not None and n[:-2] == str(season):
                    episode = int(n[-2:])  # "Show 205" inside a Season 2 folder
                elif season is None:
                    season, episode = int(n[:-2]), int(n[-2:])
                    uncertain = True
                if episode is not None:
                    title_part = stem[: inner.start()]
        if sm and not title_part:
            title_part = stem[: sm.start()]

    title_raw, year = _split_year(title_part)
    title = _clean(title_raw)
    if not title:
        title, dir_year = _show_from_dirs(dirs)
        year = year or dir_year
    elif not year:
        year = _show_from_dirs(dirs)[1]

    if episode is not None and episode == 0 and season is None:
        episode = None

    return EpisodeGuess(
        title=title,
        year=year,
        season=season,
        episode=episode,
        episode_end=episode_end,
        uncertain=uncertain,
    )


# ---------------------------------------------------------------------------
# Output paths
# ---------------------------------------------------------------------------

_ILLEGAL = re.compile(r'[<>"/\\|?*\x00-\x1f]')


def safe_name(name: str) -> str:
    """Make a title usable as a Windows file/folder name."""
    s = name.replace(":", " -")
    s = _ILLEGAL.sub("", s)
    s = " ".join(s.split())
    return s.rstrip(" .")


def plex_movie_path(title: str, year: Optional[str], output_dir: str) -> str:
    title = safe_name(title)
    folder = f"{title} ({year})" if year else title
    return str(Path(output_dir) / folder / f"{folder}.mkv")


def flat_output_path(source: str, output_dir: str) -> str:
    """Keep the source's name exactly; only the extension becomes .mkv."""
    return str(Path(output_dir) / (Path(source).stem + ".mkv"))


def episode_tag(season: int, episode: int, episode_end: Optional[int] = None) -> str:
    tag = f"s{season:02d}e{episode:02d}"
    if episode_end is not None and episode_end > episode:
        tag += f"-e{episode_end:02d}"
    return tag


def plex_tv_path(
    title: str,
    year: Optional[str],
    season: int,
    episode: int,
    output_dir: str,
    episode_end: Optional[int] = None,
) -> str:
    show = safe_name(title)
    show_folder = f"{show} ({year})" if year else show
    season_folder = f"Season {season:02d}"
    filename = f"{show_folder} - {episode_tag(season, episode, episode_end)}.mkv"
    return str(Path(output_dir) / show_folder / season_folder / filename)

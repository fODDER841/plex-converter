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


def plex_movie_path(title: str, year: Optional[str], output_dir: str) -> str:
    folder = f"{title} ({year})" if year else title
    return str(Path(output_dir) / folder / f"{folder}.mkv")


def plex_tv_path(
    title: str,
    year: Optional[str],
    season: int,
    episode: int,
    output_dir: str,
) -> str:
    show_folder = f"{title} ({year})" if year else title
    season_folder = f"Season {season:02d}"
    filename = f"{title} - S{season:02d}E{episode:02d}.mkv"
    return str(Path(output_dir) / show_folder / season_folder / filename)

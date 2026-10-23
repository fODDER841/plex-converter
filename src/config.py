import json
import os
from pathlib import Path

_DIR = Path(os.environ.get("APPDATA", Path.home())) / "PlexConverter"
_FILE = _DIR / "settings.json"


def load() -> dict:
    try:
        return json.loads(_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save(data: dict) -> None:
    _DIR.mkdir(parents=True, exist_ok=True)
    _FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")

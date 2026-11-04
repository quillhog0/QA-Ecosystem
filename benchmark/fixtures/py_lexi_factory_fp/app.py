import json
import os
from pathlib import Path


def token() -> str:
    return os.environ.get("BOT_TOKEN", "")


def load_rows(path: Path) -> list:
    return json.loads(path.read_text(encoding="utf-8"))

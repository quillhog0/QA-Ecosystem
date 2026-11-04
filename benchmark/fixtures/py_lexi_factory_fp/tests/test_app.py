import os
from pathlib import Path

import app


def test_token_reads_env(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "abc")
    assert app.token() == "abc"


def test_load_rows():
    rows = app.load_rows(Path(__file__).parent / "fixtures" / "rows.json")
    assert rows[0]["amount"] == 1

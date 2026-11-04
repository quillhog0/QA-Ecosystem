from core.toolchain import detect_languages


def _mixed(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    return tmp_path


def test_auto_detects_both(tmp_path, monkeypatch):
    monkeypatch.delenv("QA_LANGUAGES", raising=False)
    assert detect_languages(_mixed(tmp_path)) == ["python", "ts"]


def test_python_only_selection(tmp_path, monkeypatch):
    monkeypatch.setenv("QA_LANGUAGES", "python")
    assert detect_languages(_mixed(tmp_path)) == ["python"]


def test_ts_only_selection(tmp_path, monkeypatch):
    monkeypatch.setenv("QA_LANGUAGES", "ts")
    assert detect_languages(_mixed(tmp_path)) == ["ts"]

"""Field-test harness: parsing, clone policy, evaluation and rendering.

Everything here is offline. No git command and no ``main.py`` run is spawned:
the harness' process runner is replaced with a fake that records the command
lines, so the tests assert *what would be run* as well as what is reported.
"""

from __future__ import annotations

import types
from pathlib import Path

import pytest
import yaml

from benchmark import field_test

SHA = "35776b6d9b5690e047b497cd80290c59cb87c90c"
OTHER_SHA = "0123456789abcdef0123456789abcdef01234567"


def _proc(stdout: str = "", stderr: str = "", returncode: int = 0):
    return types.SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


def _write_yaml(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def _entry(name: str = "widget", sha: str = SHA, url: str = "https://example.invalid/widget", docs: str = "") -> dict:
    return {"name": name, "url": url, "sha": sha, "docs": docs}


def _row(**overrides) -> dict:
    base = {
        "name": "widget",
        "sha": SHA,
        "verdict": "CLEAR",
        "sandbox": "bwrap",
        "exit_code": 0,
        "collected": 10,
        "passed": 10,
        "failed": 0,
        "skipped": 0,
        "errors": 0,
        "cause": "",
        "status": "OK",
    }
    base.update(overrides)
    return base


def _fake_runner(monkeypatch, *, head: str = SHA, git_fails: str = "", main_yaml=None,
                 main_stdout: str = "", main_returncode: int = 0, main_stderr: str = "",
                 timeout: bool = False) -> list[list[str]]:
    """Replace the harness runner; return the list of recorded command lines."""
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if cmd[0] == "git":
            # Match a subcommand token, never the joined line: a test's temp path
            # is named after the test and would otherwise contain it.
            if git_fails and git_fails in cmd:
                return _proc(stderr="fatal: simulated git failure", returncode=128)
            if "rev-parse" in cmd:
                return _proc(stdout=f"{head}\n")
            return _proc()
        if timeout:
            raise field_test.subprocess.TimeoutExpired(cmd=list(cmd), timeout=1)
        stdout = main_stdout
        if main_yaml is not None:
            reports_dir = Path(cmd[cmd.index("--reports-dir") + 1])
            reports_dir.mkdir(parents=True, exist_ok=True)
            _write_yaml(reports_dir / "run-1.yaml", main_yaml)
            stdout = f"verdict={main_yaml.get('verdict', '')}\nreport={reports_dir / 'run-1.md'}\n"
        return _proc(stdout=stdout, stderr=main_stderr, returncode=main_returncode)

    monkeypatch.setattr("benchmark.field_test.subprocess.run", fake_run)
    return calls


# --- manifest parsing -------------------------------------------------------
def test_load_manifest_normalises_entries(tmp_path):
    path = _write_yaml(tmp_path / "repos.yaml", {"repos": [
        {"name": " widget ", "sha": f" {SHA} ", "url": " https://example.invalid/w ", "docs": " docs/index.md "},
        {"name": "gadget", "sha": OTHER_SHA},
    ]})
    rows = field_test.load_manifest(path)
    assert rows == [
        {"name": "widget", "url": "https://example.invalid/w", "sha": SHA, "docs": "docs/index.md"},
        {"name": "gadget", "url": "", "sha": OTHER_SHA, "docs": ""},
    ]


def test_load_manifest_accepts_the_shipped_pins():
    rows = field_test.load_manifest(field_test.MANIFEST)
    assert len(rows) == 10
    assert all(len(row["sha"]) == 40 for row in rows)
    assert all(row["url"].startswith("https://") for row in rows)
    assert len({row["name"] for row in rows}) == 10


@pytest.mark.parametrize("data", [
    {"repos": []},
    {"repos": "widget"},
    {},
    [],
    {"repos": ["widget"]},
    {"repos": [{"name": "widget"}]},
    {"repos": [{"sha": SHA}]},
    {"repos": [_entry(), _entry()]},
])
def test_load_manifest_rejects_malformed_input(tmp_path, data):
    path = _write_yaml(tmp_path / "repos.yaml", data)
    with pytest.raises(SystemExit):
        field_test.load_manifest(path)


def test_load_manifest_rejects_an_empty_file(tmp_path):
    path = tmp_path / "repos.yaml"
    path.write_text("", encoding="utf-8")
    with pytest.raises(SystemExit):
        field_test.load_manifest(path)


# --- manifest validation (the manifest is a boundary, not a suggestion) -----
@pytest.mark.parametrize("field,value,needle", [
    # name becomes a directory under the clone root.
    ("name", "../widget", "'..'"),
    ("name", "widget/../x", "'..'"),
    ("name", "my widget", "^[A-Za-z0-9._-]+$"),
    ("name", "widget;rm", "^[A-Za-z0-9._-]+$"),
    ("name", "widget\nrm -rf /", "^[A-Za-z0-9._-]+$"),
    ("name", "widget/", "^[A-Za-z0-9._-]+$"),
    ("name", "", "name and sha"),
    # sha becomes a git argument and has to be a full commit.
    ("sha", SHA[:39], "40-character lowercase commit hash"),
    ("sha", SHA.upper(), "40-character lowercase commit hash"),
    ("sha", "g" * 40, "40-character lowercase commit hash"),
    ("sha", SHA + "0", "40-character lowercase commit hash"),
    ("sha", "--depth", "40-character lowercase commit hash"),
    ("sha", "", "name and sha"),
    # url is handed to `git remote add` / `set-url`.
    ("url", "http://example.invalid/widget", "must start with https://"),
    ("url", "ssh://git@example.invalid/widget", "must start with https://"),
    ("url", "file:///tmp/clone", "must start with https://"),
    ("url", "-uhttps://example.invalid/widget", "must start with https://"),
    # docs is a path inside the clone.
    ("docs", "/etc/passwd", "path inside the clone"),
    ("docs", "../secrets.md", "path inside the clone"),
])
def test_load_manifest_rejects_a_field_that_could_escape_its_shape(tmp_path, field, value, needle):
    path = _write_yaml(tmp_path / "repos.yaml", {"repos": [_entry(**{field: value})]})
    with pytest.raises(SystemExit) as excinfo:
        field_test.load_manifest(path)
    assert needle in str(excinfo.value)


def test_load_manifest_accepts_a_valid_shape(tmp_path):
    path = _write_yaml(tmp_path / "repos.yaml", {"repos": [
        _entry(name="a_b.c-d", url="https://example.invalid/a.git", docs="README.rst"),
    ]})
    assert field_test.load_manifest(path)[0]["name"] == "a_b.c-d"


def test_load_manifest_accepts_a_missing_url(tmp_path):
    """No url is legitimate: the entry can only be satisfied from --repos-dir."""
    path = _write_yaml(tmp_path / "repos.yaml", {"repos": [_entry(url="")]})
    assert field_test.load_manifest(path)[0]["url"] == ""


def test_the_shipped_manifest_needs_no_relaxation():
    """Every shipped pin satisfies the strict validation above."""
    for row in field_test.load_manifest(field_test.MANIFEST):
        assert field_test.SHA_RE.match(row["sha"])
        assert field_test.NAME_RE.match(row["name"]) and ".." not in row["name"]
        assert row["url"].startswith(field_test.URL_PREFIX)


# --- expectations -----------------------------------------------------------
def test_load_expected_missing_file_means_observe_only(tmp_path):
    assert field_test.load_expected(tmp_path / "absent.yaml") == {}


def test_load_expected_accepts_a_verdict_shorthand(tmp_path):
    path = _write_yaml(tmp_path / "exp.yaml", {"expectations": {
        "widget": "CLEAR",
        "gadget": {"verdict": "BLOCKED_ENV", "exit_code": 2, "cause": "missing dependency: trio"},
    }})
    assert field_test.load_expected(path) == {
        "widget": {"verdict": "CLEAR"},
        "gadget": {"verdict": "BLOCKED_ENV", "exit_code": 2, "cause": "missing dependency: trio"},
    }


@pytest.mark.parametrize("data", [
    {"expectations": []},
    {},
    {"expectations": {"widget": 3}},
    {"expectations": {"widget": ["CLEAR"]}},
])
def test_load_expected_rejects_malformed_input(tmp_path, data):
    path = _write_yaml(tmp_path / "exp.yaml", data)
    with pytest.raises(SystemExit):
        field_test.load_expected(path)


def test_the_shipped_expectations_cover_every_pin():
    rows = field_test.load_manifest(field_test.MANIFEST)
    expected = field_test.load_expected(field_test.EXPECTED)
    assert set(expected) == {row["name"] for row in rows}
    verdicts = [item["verdict"] for item in expected.values()]
    assert verdicts.count("CLEAR") == 5
    assert verdicts.count("BLOCKED_ENV") == 5
    assert "VETO" not in verdicts
    for item in expected.values():
        assert item["exit_code"] == (0 if item["verdict"] == "CLEAR" else 2)


# --- selection --------------------------------------------------------------
def test_select_entries_without_a_filter_keeps_the_manifest_order():
    entries = [_entry("a"), _entry("b")]
    selected = field_test.select_entries(entries, "  ")
    assert selected == entries
    assert selected is not entries


def test_select_entries_follows_the_requested_order():
    entries = [_entry("a"), _entry("b"), _entry("c")]
    assert [e["name"] for e in field_test.select_entries(entries, " c , a ")] == ["c", "a"]


def test_select_entries_rejects_an_unknown_name():
    with pytest.raises(SystemExit) as excinfo:
        field_test.select_entries([_entry("a")], "a,ghost")
    assert "ghost" in str(excinfo.value)


# --- docs resolution --------------------------------------------------------
def test_resolve_docs_prefers_the_manifest_entry(tmp_path):
    (tmp_path / "README.md").write_text("readme", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "index.md").write_text("index", encoding="utf-8")
    assert field_test.resolve_docs(tmp_path, _entry(docs="docs/index.md")) == tmp_path / "docs" / "index.md"


def test_resolve_docs_does_not_fall_back_when_the_named_file_is_absent(tmp_path):
    (tmp_path / "README.md").write_text("readme", encoding="utf-8")
    assert field_test.resolve_docs(tmp_path, _entry(docs="docs/index.md")) is None


@pytest.mark.parametrize("name", ["README.md", "README.rst", "README.txt"])
def test_resolve_docs_finds_each_readme_spelling(tmp_path, name):
    (tmp_path / name).write_text("readme", encoding="utf-8")
    assert field_test.resolve_docs(tmp_path, _entry()) == tmp_path / name


def test_resolve_docs_prefers_markdown_over_rst(tmp_path):
    (tmp_path / "README.rst").write_text("rst", encoding="utf-8")
    (tmp_path / "README.md").write_text("md", encoding="utf-8")
    assert field_test.resolve_docs(tmp_path, _entry()).name == "README.md"


def test_resolve_docs_returns_none_without_any_readme(tmp_path):
    assert field_test.resolve_docs(tmp_path, _entry()) is None


# --- text helpers -----------------------------------------------------------
def test_one_line_collapses_whitespace_and_truncates():
    assert field_test._one_line("  a\n b\t c  ") == "a b c"
    long = "x" * 250
    assert field_test._one_line(long) == "x" * 200 + "..."
    assert field_test._one_line("short", 3) == "sho..."
    assert field_test._one_line(None) == ""


# --- clone policy -----------------------------------------------------------
def test_head_sha_reads_the_commit(monkeypatch, tmp_path):
    calls = _fake_runner(monkeypatch, head=SHA)
    assert field_test.head_sha(tmp_path) == SHA
    assert calls == [["git", "-C", str(tmp_path), "rev-parse", "HEAD"]]


def test_head_sha_is_empty_when_git_fails(monkeypatch, tmp_path):
    _fake_runner(monkeypatch, git_fails="rev-parse")
    assert field_test.head_sha(tmp_path) == ""


def test_ensure_repo_reuses_a_pinned_clone_without_writing_to_it(monkeypatch, tmp_path):
    repos = tmp_path / "clones"
    (repos / "widget").mkdir(parents=True)
    (repos / "widget" / "README.md").write_text("readme", encoding="utf-8")
    calls = _fake_runner(monkeypatch)
    repo, note = field_test.ensure_repo(_entry(), repos_dir=repos, cache_dir=tmp_path / "cache", offline=True)
    assert repo == repos / "widget"
    assert note == ""
    assert all("rev-parse" in cmd for cmd in calls)  # read-only: never fetch, never checkout


def test_ensure_repo_reports_a_sha_mismatch_but_still_returns_the_clone(monkeypatch, tmp_path):
    repos = tmp_path / "clones"
    (repos / "widget").mkdir(parents=True)
    _fake_runner(monkeypatch, head=OTHER_SHA)
    repo, note = field_test.ensure_repo(_entry(), repos_dir=repos, cache_dir=tmp_path / "cache", offline=True)
    assert repo == repos / "widget"
    assert note == f"sha mismatch: HEAD {OTHER_SHA} != pin {SHA}"


def test_ensure_repo_falls_back_to_the_cache_directory(monkeypatch, tmp_path):
    cache = tmp_path / "cache"
    (cache / "widget").mkdir(parents=True)
    _fake_runner(monkeypatch)
    repo, note = field_test.ensure_repo(
        _entry(), repos_dir=tmp_path / "clones", cache_dir=cache, offline=True
    )
    assert repo == cache / "widget"
    assert note == ""


def test_a_sha_mismatch_in_the_cache_is_not_reused(monkeypatch, tmp_path):
    cache = tmp_path / "cache"
    (cache / "widget").mkdir(parents=True)
    _fake_runner(monkeypatch, head=OTHER_SHA)
    repo, note = field_test.ensure_repo(
        _entry(), repos_dir=tmp_path / "clones", cache_dir=cache, offline=True
    )
    assert repo is None
    assert "--offline forbids fetching" in note


def test_offline_refuses_to_fetch_a_missing_clone(monkeypatch, tmp_path):
    calls = _fake_runner(monkeypatch)
    repo, note = field_test.ensure_repo(
        _entry(), repos_dir=tmp_path / "clones", cache_dir=tmp_path / "cache", offline=True
    )
    assert repo is None
    assert "no clone at" in note
    assert calls == []  # not even a probe: nothing to inspect


def test_a_missing_url_is_reported_instead_of_guessed(monkeypatch, tmp_path):
    _fake_runner(monkeypatch)
    repo, note = field_test.ensure_repo(
        _entry(url=""), repos_dir=tmp_path / "clones", cache_dir=tmp_path / "cache", offline=False
    )
    assert repo is None
    assert "manifest has no url" in note


def test_ensure_repo_fetches_exactly_the_pinned_commit(monkeypatch, tmp_path):
    cache = tmp_path / "cache"
    calls = _fake_runner(monkeypatch)
    repo, note = field_test.ensure_repo(
        _entry(), repos_dir=tmp_path / "clones", cache_dir=cache, offline=False
    )
    target = cache / "widget"
    assert repo == target
    assert note == ""
    assert calls == [
        ["git", "init", "--quiet", "--", str(target)],
        ["git", "-C", str(target), "remote", "add", "origin", "https://example.invalid/widget"],
        ["git", "-C", str(target), "fetch", "--depth", "1", "origin", SHA],
        ["git", "-C", str(target), "checkout", "--quiet", "--detach", "FETCH_HEAD"],
        ["git", "-C", str(target), "rev-parse", "HEAD"],
    ]


def test_ensure_repo_reuses_an_existing_cache_git_dir(monkeypatch, tmp_path):
    target = tmp_path / "cache" / "widget"
    (target / ".git").mkdir(parents=True)
    # The cached clone sits at another commit, so the fetch path is taken and the
    # existing repository only needs its remote refreshed.
    calls = _fake_runner(monkeypatch, head=OTHER_SHA)
    field_test.ensure_repo(_entry(), repos_dir=tmp_path / "clones", cache_dir=tmp_path / "cache", offline=False)
    assert calls[0] == ["git", "-C", str(target), "rev-parse", "HEAD"]
    assert calls[1] == [
        "git", "-C", str(target), "remote", "set-url", "origin", "https://example.invalid/widget",
    ]
    assert calls[2] == ["git", "-C", str(target), "fetch", "--depth", "1", "origin", SHA]
    assert not any(cmd[1] == "init" for cmd in calls)


def test_a_failed_fetch_is_reported_with_the_command(monkeypatch, tmp_path):
    calls = _fake_runner(monkeypatch, git_fails="fetch")
    repo, note = field_test.ensure_repo(
        _entry(), repos_dir=tmp_path / "clones", cache_dir=tmp_path / "cache", offline=False
    )
    assert repo is None
    assert note.startswith("clone failed (git -C")
    assert "simulated git failure" in note
    assert not any("checkout" in cmd for cmd in calls)  # stopped at the first failure


def test_a_wrong_commit_after_fetch_is_reported(monkeypatch, tmp_path):
    _fake_runner(monkeypatch, head=OTHER_SHA)
    repo, note = field_test.ensure_repo(
        _entry(), repos_dir=tmp_path / "clones", cache_dir=tmp_path / "cache", offline=False
    )
    assert repo == tmp_path / "cache" / "widget"
    assert note == f"sha mismatch after fetch: HEAD {OTHER_SHA} != pin {SHA}"


def test_ensure_repo_refuses_a_malformed_sha_even_if_the_manifest_was_bypassed(monkeypatch, tmp_path):
    calls = _fake_runner(monkeypatch)
    with pytest.raises(SystemExit) as excinfo:
        field_test.ensure_repo(
            _entry(sha="--depth"), repos_dir=tmp_path / "clones",
            cache_dir=tmp_path / "cache", offline=False,
        )
    assert "not a full commit hash" in str(excinfo.value)
    assert calls == []


def test_ensure_repo_refuses_a_non_https_url_even_if_the_manifest_was_bypassed(monkeypatch, tmp_path):
    calls = _fake_runner(monkeypatch)
    with pytest.raises(SystemExit) as excinfo:
        field_test.ensure_repo(
            _entry(url="file:///tmp/clone"), repos_dir=tmp_path / "clones",
            cache_dir=tmp_path / "cache", offline=False,
        )
    assert "must start with https://" in str(excinfo.value)
    assert calls == []


# --- root cause extraction --------------------------------------------------
def test_root_cause_prefers_the_post_audit_root_cause():
    report = {
        "post_audit": {"root_cause": "missing dependency: trio", "riley": {"root_cause": "older"}},
        "root_cause": "oldest",
        "role_results": [{"verdict": "VETO", "summary": "role text"}],
    }
    assert field_test.root_cause_of(report) == "missing dependency: trio"
    assert field_test.root_cause_of({"post_audit": {"riley": {"root_cause": "riley text"}}}) == "riley text"
    assert field_test.root_cause_of({"root_cause": "report text"}) == "report text"


def test_root_cause_falls_back_to_the_first_accusing_role():
    report = {"role_results": [
        {"verdict": "CLEAR", "summary": "fine"},
        {"verdict": "PENDING", "summary": "not run"},
        {"verdict": "", "summary": "empty"},
        {"verdict": "BLOCKED_ENV", "summary": "generated module absent: humanize._version"},
        {"verdict": "VETO", "summary": "later"},
    ]}
    assert field_test.root_cause_of(report) == "generated module absent: humanize._version"


def test_root_cause_is_empty_for_a_clean_report():
    assert field_test.root_cause_of({"verdict": "CLEAR", "role_results": [{"verdict": "CLEAR", "summary": "ok"}]}) == ""
    assert field_test.root_cause_of({}) == ""


# --- running one repo -------------------------------------------------------
def _report_yaml(**overrides) -> dict:
    base = {
        "verdict": "CLEAR",
        "sandbox": "bwrap",
        "execution": {"collected": 10, "passed": 9, "failed": 0, "skipped": 1, "errors": 0,
                      "exit_code": 0, "install_status": "ok"},
        "post_audit": {},
    }
    base.update(overrides)
    return base


def test_run_repo_reads_the_yaml_report(monkeypatch, tmp_path):
    calls = _fake_runner(monkeypatch, main_yaml=_report_yaml())
    docs = tmp_path / "README.md"
    docs.write_text("readme", encoding="utf-8")
    row = field_test.run_repo(tmp_path / "widget", docs, tmp_path / "out", timeout=30)
    assert row["verdict"] == "CLEAR"
    assert row["sandbox"] == "bwrap"
    assert row["exit_code"] == 0
    assert (row["collected"], row["passed"], row["skipped"]) == (10, 9, 1)
    assert row["pytest_exit"] == 0
    assert row["install_status"] == "ok"
    assert row["error"] == ""
    assert row["report"].endswith("run-1.md")
    cmd = calls[0]
    assert cmd[1].endswith("main.py")
    assert cmd[cmd.index("--project") + 1] == str(tmp_path / "widget")
    assert cmd[cmd.index("--docs") + 1] == str(docs)
    assert "--self-test" not in cmd and "--mutation" not in cmd


def test_run_repo_reports_an_environmental_cause_and_exit_two(monkeypatch, tmp_path):
    report = _report_yaml(
        verdict="BLOCKED_ENV",
        execution={"collected": 0, "passed": 0, "failed": 0, "errors": 1, "exit_code": 2,
                   "install_status": "no dependency manifest"},
        post_audit={"root_cause": "missing dependency: hypothesis"},
    )
    _fake_runner(monkeypatch, main_yaml=report, main_returncode=2)
    row = field_test.run_repo(tmp_path / "widget", tmp_path / "README.md", tmp_path / "out", timeout=30)
    assert row["verdict"] == "BLOCKED_ENV"
    assert row["exit_code"] == 2
    assert row["cause"] == "missing dependency: hypothesis"
    assert row["errors"] == 1


def test_run_repo_records_a_timeout_without_raising(monkeypatch, tmp_path):
    _fake_runner(monkeypatch, timeout=True)
    row = field_test.run_repo(tmp_path / "widget", tmp_path / "README.md", tmp_path / "out", timeout=12)
    assert row["verdict"] == "TIMEOUT"
    assert row["error"] == "main.py exceeded 12s"
    assert row["collected"] == 0


def test_run_repo_keeps_stderr_when_no_report_was_written(monkeypatch, tmp_path):
    _fake_runner(monkeypatch, main_stdout="verdict=VETO\n", main_returncode=1, main_stderr="Traceback\nboom")
    row = field_test.run_repo(tmp_path / "widget", tmp_path / "README.md", tmp_path / "out", timeout=30)
    assert row["verdict"] == "VETO"  # stdout fallback
    assert row["exit_code"] == 1
    assert row["error"] == "Traceback boom"
    assert row["report"] == ""


# --- expectation evaluation -------------------------------------------------
def test_evaluate_row_ok_when_everything_matches():
    status, detail = field_test.evaluate_row(
        _row(), {"verdict": "CLEAR", "exit_code": 0, "min_collected": 10, "min_passed": 10}
    )
    assert (status, detail) == ("OK", "matches expectation")


def test_evaluate_row_observed_without_an_expectation():
    assert field_test.evaluate_row(_row(), None) == ("OBSERVED", "no expectation recorded")
    assert field_test.evaluate_row(_row(), {})[0] == "OBSERVED"


def test_evaluate_row_regression_on_a_harness_error():
    status, detail = field_test.evaluate_row(_row(error="main.py exceeded 900s"), {"verdict": "CLEAR"})
    assert (status, detail) == ("REGRESSION", "main.py exceeded 900s")


def test_evaluate_row_regression_on_a_wrong_verdict():
    status, detail = field_test.evaluate_row(_row(verdict="VETO", exit_code=1), {"verdict": "CLEAR", "exit_code": 0})
    assert status == "REGRESSION"
    assert "verdict 'VETO' not in ['CLEAR']" in detail
    assert "exit 1 != 0" in detail


def test_evaluate_row_accepts_a_list_of_verdicts():
    expectation = {"accept_verdicts": ["CLEAR", "BLOCKED_ENV"], "exit_code": 2}
    assert field_test.evaluate_row(_row(verdict="BLOCKED_ENV", exit_code=2), expectation)[0] == "OK"
    assert field_test.evaluate_row(_row(verdict="VETO", exit_code=2), expectation)[0] == "REGRESSION"


def test_evaluate_row_requires_the_cause_substring():
    row = _row(verdict="BLOCKED_ENV", exit_code=2, cause="generated module absent: humanize._version")
    expectation = {"verdict": "BLOCKED_ENV", "exit_code": 2, "cause": "missing dependency: hypothesis"}
    status, detail = field_test.evaluate_row(row, expectation)
    assert status == "REGRESSION"
    assert "cause 'missing dependency: hypothesis' not in" in detail
    expectation["cause"] = "generated module absent"
    assert field_test.evaluate_row(row, expectation)[0] == "OK"


@pytest.mark.parametrize("key,value", [("collected", 9), ("passed", 9), ("failed", 1), ("skipped", 1), ("errors", 1)])
def test_evaluate_row_enforces_every_count_floor(key, value):
    status, detail = field_test.evaluate_row(_row(**{key: value}), {f"min_{key}": 10})
    assert status == "REGRESSION"
    assert detail == f"{key} {value} < 10"


def test_evaluate_row_counts_are_floors_not_equalities():
    assert field_test.evaluate_row(_row(collected=40, passed=40), {"min_collected": 10})[0] == "OK"


def test_a_blocked_row_is_never_accepted_as_clear():
    status, detail = field_test.evaluate_row(_row(verdict="BLOCKED_ENV", exit_code=2), {"verdict": "CLEAR"})
    assert status == "REGRESSION"
    assert "BLOCKED_ENV" in detail


# --- rendering --------------------------------------------------------------
def test_render_table_has_one_row_per_repo_and_escapes_pipes():
    rows = [
        _row(cause="a | b", expected="CLEAR", detail="matches expectation"),
        _row(name="gadget", sha=OTHER_SHA, verdict="", sandbox="", cause="", status="REGRESSION",
             note="sha mismatch: HEAD unknown != pin", expected="", exit_code="-"),
    ]
    table = field_test.render_table(rows)
    lines = table.splitlines()
    assert len(lines) == 4
    assert lines[0].startswith("| repo | sha | verdict | sandbox | collected |")
    assert set(lines[1].split("|")) == {"", "---"}
    assert f"| widget | {SHA[:12]} | CLEAR | bwrap | 10 | 10 | 0 | 0 | CLEAR | OK | a \\| b |" == lines[2]
    assert f"| gadget | {OTHER_SHA[:12]} | - | - | 10 | 10 | 0 | - | - | REGRESSION | sha mismatch" in lines[3]


def test_render_markdown_tallies_verdicts_and_states_the_network_policy(tmp_path):
    rows = [
        _row(status="OK", expected="CLEAR", detail="matches expectation", docs="/clones/widget/README.md"),
        _row(name="gadget", verdict="BLOCKED_ENV", exit_code=2, status="OK", expected="BLOCKED_ENV",
             cause="missing dependency: trio", note="sha mismatch: HEAD x != pin y", detail="matches expectation"),
    ]
    text = field_test.render_markdown(rows, "20261102T000000Z", offline=True, repos_dir=tmp_path)
    assert "# Field test 20261102T000000Z" in text
    assert "- verdicts: 1 BLOCKED_ENV, 1 CLEAR" in text
    assert "- network: offline (clones reused, nothing fetched)" in text
    assert "allow_network=false" in text
    assert f"- repos dir: `{tmp_path}`" in text
    assert "### gadget" in text
    assert "- clone note: sha mismatch: HEAD x != pin y" in text
    assert "- cause: missing dependency: trio" in text
    online = field_test.render_markdown(rows, "20261102T000000Z", offline=False, repos_dir=tmp_path)
    assert "- network: shallow pinned fetch allowed for missing clones" in online


def test_render_markdown_counts_an_unrun_repo_as_no_verdict(tmp_path):
    text = field_test.render_markdown([_row(verdict="", status="REGRESSION")], "ts", offline=True, repos_dir=tmp_path)
    assert "- verdicts: 1 -" in text


# --- main() wiring ----------------------------------------------------------
def _main_setup(tmp_path, monkeypatch, *, expectations=None, head=SHA, main_yaml=None,
                main_returncode=0, docs_name="README.md", repos=("widget",)):
    monkeypatch.setattr(field_test, "ROOT", tmp_path)
    manifest = _write_yaml(tmp_path / "repos.yaml", {"repos": [_entry(name) for name in repos]})
    clone_root = tmp_path / "clones"
    for name in repos:
        (clone_root / name).mkdir(parents=True)
        if docs_name:
            (clone_root / name / docs_name).write_text("readme", encoding="utf-8")
    expected_path = tmp_path / "expected.yaml"
    if expectations is not None:
        _write_yaml(expected_path, {"expectations": expectations})
    calls = _fake_runner(monkeypatch, head=head, main_yaml=main_yaml, main_returncode=main_returncode)
    argv = [
        "--manifest", str(manifest),
        "--expected", str(expected_path),
        "--repos-dir", str(clone_root),
        "--cache-dir", str(tmp_path / "cache"),
        "--reports-dir", str(tmp_path / "runs"),
        "--offline",
    ]
    return argv, calls


def _written_report(out: str) -> Path:
    """The markdown report path main() printed for this run."""
    line = [line for line in out.splitlines() if line.startswith("report=")][0]
    return Path(line.partition("=")[2])


def _row_status(report: Path, repo: str = "widget") -> str:
    """Status of one repo as rendered in the report's Details section."""
    marker = f"### {repo}\n"
    text = report.read_text(encoding="utf-8")
    section = text.split(marker, 1)[1]
    line = [item for item in section.splitlines() if item.startswith("- status: ")][0]
    return line.split("**")[1]


def test_main_returns_zero_when_every_row_matches(tmp_path, monkeypatch, capsys):
    argv, calls = _main_setup(
        tmp_path, monkeypatch,
        expectations={"widget": {"verdict": "CLEAR", "exit_code": 0, "min_collected": 10}},
        main_yaml=_report_yaml(),
    )
    assert field_test.main(argv) == 0
    out = capsys.readouterr().out
    assert "summary=1 OK / 0 OBSERVED / 0 REGRESSION / 0 ERROR" in out
    assert "| widget |" in out
    report_line = [line for line in out.splitlines() if line.startswith("report=")][0]
    written = Path(report_line.partition("=")[2])
    assert written.is_file()
    assert written.parent == tmp_path / "reports"
    assert "matches expectation" in written.read_text(encoding="utf-8")
    assert sum(1 for cmd in calls if cmd[0] != "git") == 1


def test_main_returns_one_on_a_regression(tmp_path, monkeypatch, capsys):
    argv, _ = _main_setup(
        tmp_path, monkeypatch,
        expectations={"widget": {"verdict": "CLEAR", "exit_code": 0}},
        main_yaml=_report_yaml(verdict="VETO"),
        main_returncode=1,
    )
    assert field_test.main(argv) == 1
    assert "summary=0 OK / 0 OBSERVED / 1 REGRESSION / 0 ERROR" in capsys.readouterr().out


def test_main_without_an_expected_file_observes(tmp_path, monkeypatch, capsys):
    argv, _ = _main_setup(tmp_path, monkeypatch, main_yaml=_report_yaml())
    assert field_test.main(argv) == 0
    assert "summary=0 OK / 1 OBSERVED / 0 REGRESSION / 0 ERROR" in capsys.readouterr().out


def test_main_never_runs_a_clone_whose_commit_is_wrong(tmp_path, monkeypatch, capsys):
    argv, calls = _main_setup(
        tmp_path, monkeypatch, head=OTHER_SHA,
        expectations={"widget": {"verdict": "CLEAR"}}, main_yaml=_report_yaml(),
    )
    assert field_test.main(argv) == 1
    out = capsys.readouterr().out
    assert "summary=0 OK / 0 OBSERVED / 0 REGRESSION / 1 ERROR" in out
    assert not any(cmd[0] != "git" for cmd in calls)  # main.py was never invoked
    written = _written_report(out)
    text = written.read_text(encoding="utf-8")
    assert "pin not satisfied" in text
    assert f"HEAD {OTHER_SHA} != {SHA}" in text


def test_a_wrong_commit_is_an_error_even_when_the_expectation_would_match(tmp_path, monkeypatch, capsys):
    """The pin check happens before anything runs, so a clone at another commit
    can be reported neither as OK nor as a QA regression - only as an error."""
    argv, calls = _main_setup(
        tmp_path, monkeypatch, head=OTHER_SHA,
        expectations={"widget": {"verdict": "CLEAR"}}, main_yaml=_report_yaml(),
    )
    assert field_test.main(argv) == 1
    assert _row_status(_written_report(capsys.readouterr().out)) == "ERROR"
    assert not any(cmd[0] != "git" for cmd in calls)


def test_main_reports_a_missing_docs_file(tmp_path, monkeypatch, capsys):
    argv, calls = _main_setup(tmp_path, monkeypatch, docs_name="", main_yaml=_report_yaml())
    assert field_test.main(argv) == 1
    out = capsys.readouterr().out
    assert "1 ERROR" in out
    written = Path([line for line in out.splitlines() if line.startswith("report=")][0].partition("=")[2])
    assert "docs file not found" in written.read_text(encoding="utf-8")
    assert not any(cmd[0] != "git" for cmd in calls)


def test_main_only_runs_the_selected_repos(tmp_path, monkeypatch, capsys):
    argv, calls = _main_setup(
        tmp_path, monkeypatch, repos=("widget", "gadget"),
        expectations={"widget": {"verdict": "CLEAR"}, "gadget": {"verdict": "CLEAR"}},
        main_yaml=_report_yaml(),
    )
    argv += ["--only", "gadget"]
    assert field_test.main(argv) == 0
    out = capsys.readouterr().out
    assert "summary=1 OK / 0 OBSERVED / 0 REGRESSION / 0 ERROR" in out
    assert "| gadget |" in out and "| widget |" not in out
    assert len([cmd for cmd in calls if cmd[0] != "git"]) == 1


def test_main_marks_an_unpinned_cache_clone_as_an_error(tmp_path, monkeypatch, capsys):
    argv, calls = _main_setup(
        tmp_path, monkeypatch, repos=("widget", "gadget"),
        expectations={"widget": {"verdict": "CLEAR"}, "gadget": {"verdict": "CLEAR"}},
        main_yaml=_report_yaml(),
    )
    # gadget exists only in the cache with the wrong commit: the row is reported
    # as an error and never run against another revision.
    (tmp_path / "cache" / "gadget").mkdir(parents=True)
    monkeypatch.setattr(field_test, "head_sha", lambda repo: OTHER_SHA if repo.name == "gadget" else SHA)
    argv.remove("--offline")
    assert field_test.main(argv) == 1
    out = capsys.readouterr().out
    assert "summary=1 OK / 0 OBSERVED / 0 REGRESSION / 1 ERROR" in out
    assert len([cmd for cmd in calls if cmd[0] != "git"]) == 1  # only widget ran


def test_main_reports_an_unavailable_clone_as_an_error(tmp_path, monkeypatch, capsys):
    argv, calls = _main_setup(
        tmp_path, monkeypatch, expectations={"widget": {"verdict": "CLEAR"}},
        main_yaml=_report_yaml(),
    )
    for path in (tmp_path / "clones" / "widget").iterdir():
        path.unlink()
    (tmp_path / "clones" / "widget").rmdir()
    assert field_test.main(argv) == 1
    out = capsys.readouterr().out
    assert "summary=0 OK / 0 OBSERVED / 0 REGRESSION / 1 ERROR" in out
    assert "--offline forbids fetching" in _written_report(out).read_text(encoding="utf-8")
    assert not any(cmd[0] != "git" for cmd in calls)
"""Task 14 (v0.4.1) - Lexi false positives seen on two field projects.

1. git commit hashes are never code symbols;
2. env-var candidates come only from an explicit env context;
3. a secret documented in the root README counts as documented even when the
   docs root is a ``docs/`` sub-directory;
4. QA certificates, plans and fixture documents make no doc-vs-code claims, and
   a name present in a repository JSON data file is never a P1 veto.
Each rule is pinned for a Python and a TypeScript project.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agents.inventory import is_non_claim_doc, list_claim_docs, list_docs
from agents.lexi import _heuristic_drift
from agents.lexi_claims import (
    _data_file_text,
    _root_readmes,
    check_call_form_claims,
    check_env_vars,
    doc_env_candidates,
    env_example_keys,
    is_commit_hash,
)

PY_SRC = ("app.py", 'import os\n\ndef token():\n    return os.environ.get("BOT_TOKEN", "")\n')
TS_SRC = ("src/index.ts", "export function token(): string {\n  return process.env.BOT_TOKEN ?? '';\n}\n")
LANGS = pytest.mark.parametrize("src", [PY_SRC, TS_SRC], ids=["py", "ts"])


def _project(tmp_path: Path, src: tuple[str, str], docs: dict[str, str], extra: dict[str, str] | None = None) -> Path:
    rel, text = src
    target = tmp_path / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    (tmp_path / "docs").mkdir(exist_ok=True)
    for name, body in {**docs, **(extra or {})}.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return tmp_path


def _findings(root: Path):
    report = _heuristic_drift(docs_root=root / "docs", project_root=root, config={})
    return {(f.kind, f.path): f.severity for f in report.findings}


# 1. commit hashes ---------------------------------------------------------

@pytest.mark.parametrize("token", ["ae5a684", "ad680750c", "cc9bcfd", "0123456", "a" * 39 + "1"])
def test_commit_hashes_are_recognised(token):
    assert is_commit_hash(token)


@pytest.mark.parametrize("token", ["abc12", "deadbeef", "facade", "ae5a68g", "AE5A684", "a" * 40 + "12", "token"])
def test_non_hashes_are_not_recognised(token):
    assert not is_commit_hash(token)


def test_hash_length_boundaries():
    assert is_commit_hash("a1b2c3d")  # 7
    assert not is_commit_hash("a1b2c3")  # 6
    assert is_commit_hash("1" * 40)
    assert not is_commit_hash("1" * 41)


@LANGS
def test_backticked_commit_hash_is_no_finding(tmp_path, src):
    root = _project(tmp_path, src, {"docs/README.md": "Fixed in `ae5a684` and `ad680750c1b2`. See `token()`.\n"})
    found = _findings(root)
    assert not any(path in ("ae5a684", "ad680750c1b2") for _k, path in found)


def test_call_form_on_hash_is_ignored(tmp_path):
    root = _project(tmp_path, PY_SRC, {"docs/README.md": "Run `ae5a684.ad680750c()` and `cfg.reallyMissing()`.\n"})
    paths = [f.path for f in check_call_form_claims(root / "docs", root)]
    assert "ad680750c" not in paths
    assert "reallyMissing" in paths


# 2. env-var context -------------------------------------------------------

PLAIN = "Statuses UPDATE, SIGTERM, USDC, USDT, VERIFIED, PASS, FIFO, MERGED, BLOCKED (key = token env)."


def test_plain_uppercase_words_are_not_env_candidates():
    assert doc_env_candidates(PLAIN) == []


@pytest.mark.parametrize(
    "line, name",
    [
        ("read process.env.API_URL first", "API_URL"),
        ("process.env['API_URL']", "API_URL"),
        ('os.environ["API_URL"]', "API_URL"),
        ('os.environ.get("API_URL")', "API_URL"),
        ('os.getenv("API_URL")', "API_URL"),
        ("curl $API_URL/health", "API_URL"),
        ("curl ${API_URL}/health", "API_URL"),
        ("export API_URL=http://x", "API_URL"),
        ("set the env var API_URL", "API_URL"),
        ("set the environment variable API_URL", "API_URL"),
        ("`API_URL` env var is required", "API_URL"),
        ("API_URL environment variable", "API_URL"),
    ],
)
def test_env_context_forms_yield_candidate(line, name):
    assert doc_env_candidates(line) == [name]


def test_env_candidates_are_unique_and_uppercase_only():
    assert doc_env_candidates("$API_URL and ${API_URL} env var to") == ["API_URL"]


@LANGS
def test_plain_uppercase_words_raise_no_env_finding(tmp_path, src):
    root = _project(tmp_path, src, {"docs/README.md": PLAIN + "\n\nBOT_TOKEN env var.\n"})
    kinds = [f for f in check_env_vars(root / "docs", root) if f.kind == "undocumented_env"]
    assert kinds == []


@LANGS
def test_documented_env_in_context_not_read_is_still_reported(tmp_path, src):
    root = _project(tmp_path, src, {"docs/README.md": "export GHOST_SETTING=1\nBOT_TOKEN env var\n"})
    found = [(f.kind, f.path, f.docs_file, f.docs_line) for f in check_env_vars(root / "docs", root)]
    assert ("undocumented_env", "GHOST_SETTING", "README.md", 1) in found
    assert all(path != "BOT_TOKEN" for _k, path, _f, _l in found)


def test_env_example_keys_are_read(tmp_path):
    (tmp_path / ".env.example").write_text("# c\nAPI_URL=http://x\nexport OTHER_KEY=\nlower=1\n", encoding="utf-8")
    assert env_example_keys(tmp_path) == {"API_URL": (".env.example", 2), "OTHER_KEY": (".env.example", 3)}


def test_env_example_key_not_read_in_code_is_reported(tmp_path):
    root = _project(tmp_path, PY_SRC, {"docs/README.md": "BOT_TOKEN env var\n"}, {".env.example": "BOT_TOKEN=\nUNUSED_KEY=\n"})
    found = {(f.kind, f.path): f.docs_file for f in check_env_vars(root / "docs", root)}
    assert found.get(("undocumented_env", "UNUSED_KEY")) == ".env.example"
    assert ("undocumented_env", "BOT_TOKEN") not in found


def test_env_example_key_documents_a_secret(tmp_path):
    root = _project(tmp_path, PY_SRC, {"docs/README.md": "Nothing here.\n"}, {".env.example": "BOT_TOKEN=\n"})
    assert not [f for f in check_env_vars(root / "docs", root) if f.kind == "undocumented_secret"]


def test_ignore_list_applies_to_context_candidates(tmp_path):
    root = _project(tmp_path, PY_SRC, {"docs/README.md": "export GHOST_SETTING=1 BOT_TOKEN env var\n"})
    cfg = {"lexi": {"ignore_identifiers": ["GHOST_SETTING"]}}
    assert not [f for f in check_env_vars(root / "docs", root, config=cfg) if f.path == "GHOST_SETTING"]


def test_non_env_tokens_in_context_are_skipped(tmp_path):
    root = _project(tmp_path, PY_SRC, {"docs/README.md": "echo $HOME and $PATH; BOT_TOKEN env var\n"})
    assert not [f for f in check_env_vars(root / "docs", root) if f.kind == "undocumented_env"]


# 3. secret documented in the root README --------------------------------

@LANGS
@pytest.mark.parametrize(
    "readme",
    [
        "```bash\nBOT_TOKEN=*** npm run dev\n```\n",
        "| var | use |\n|---|---|\n| BOT_TOKEN | bot |\n",
        "Set `BOT_TOKEN`.\n",
    ],
    ids=["code-block", "table", "backtick"],
)
def test_root_readme_documents_secret_when_docs_root_is_subdir(tmp_path, src, readme):
    root = _project(tmp_path, src, {"docs/guide.md": "# Guide\n"}, {"README.md": readme})
    secrets = [f.path for f in check_env_vars(root / "docs", root) if f.kind == "undocumented_secret"]
    assert secrets == []


@LANGS
def test_truly_undocumented_secret_is_still_reported(tmp_path, src):
    root = _project(tmp_path, src, {"docs/guide.md": "# Guide\n"}, {"README.md": "# App\nUses MY_BOT_TOKEN_OLD.\n"})
    secrets = [f.path for f in check_env_vars(root / "docs", root) if f.kind == "undocumented_secret"]
    assert secrets == ["BOT_TOKEN"]


def test_root_readmes_lists_only_readme_files(tmp_path):
    for name in ("README.md", "readme.rst", "README", "README.png", "NOTES.md"):
        (tmp_path / name).write_text("x", encoding="utf-8")
    (tmp_path / "README.d").mkdir()
    assert [p.name for p in _root_readmes(tmp_path)] == ["README", "README.md", "readme.rst"]
    assert _root_readmes(tmp_path / "missing") == []


def test_root_readme_is_not_counted_twice_when_docs_root_is_project(tmp_path):
    root = _project(tmp_path, PY_SRC, {}, {"README.md": "export GHOST_SETTING=1\nBOT_TOKEN env var\n"})
    ghosts = [f for f in check_env_vars(root, root) if f.path == "GHOST_SETTING"]
    assert len(ghosts) == 1 and ghosts[0].docs_file == "README.md"


# 4. QA artefacts and data files ----------------------------------------

@pytest.mark.parametrize(
    "rel, expected",
    [
        ("QA-CERTIFICATE.md", True),
        ("qa-certificate-v2.md", True),
        ("PLAN-next.md", True),
        ("fixtures/sample.md", True),
        ("tests/fixtures/x.md", True),
        ("README.md", False),
        ("PLANNING.md", False),
        ("guide/fixtures.md", False),
        ("my-QA-CERTIFICATE.md", False),
    ],
)
def test_is_non_claim_doc(tmp_path, rel, expected):
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x", encoding="utf-8")
    assert is_non_claim_doc(path, tmp_path) is expected


def test_non_claim_doc_outside_root_uses_name(tmp_path):
    other = tmp_path / "fixtures" / "QA-CERTIFICATE.md"
    other.parent.mkdir()
    other.write_text("x", encoding="utf-8")
    assert is_non_claim_doc(other, tmp_path / "elsewhere") is True
    plain = tmp_path / "fixtures" / "README.md"
    plain.write_text("x", encoding="utf-8")
    # Outside the docs root only the file name is judged.
    assert is_non_claim_doc(plain, tmp_path / "elsewhere") is False


def test_list_claim_docs_filters_but_list_docs_keeps(tmp_path):
    for rel in ("README.md", "QA-CERTIFICATE.md", "PLAN-a.md", "fixtures/f.md"):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x", encoding="utf-8")
    assert [p.name for p in list_claim_docs(tmp_path)] == ["README.md"]
    assert len(list_docs(tmp_path)) == 4


@LANGS
def test_qa_artefacts_make_no_claims(tmp_path, src):
    root = _project(
        tmp_path,
        src,
        {
            "docs/README.md": "See `token()`.\n",
            "docs/QA-CERTIFICATE.md": "Verified `cert_helper()`; env var CERT_VAR.\n",
            "docs/PLAN-next.md": "Add `planned_helper()`.\n",
            "docs/fixtures/s.md": "Mentions `fixture_helper()`.\n",
        },
    )
    paths = {path for _k, path in _findings(root)}
    assert not paths & {"cert_helper", "planned_helper", "fixture_helper", "CERT_VAR"}


@LANGS
def test_symbol_in_json_fixture_is_not_high(tmp_path, src):
    root = _project(
        tmp_path,
        src,
        {"docs/README.md": "Rows carry `ledger_row_kind`, see `token()`.\n"},
        {"tests/fixtures/rows.json": '[{"ledger_row_kind": "swap"}]\n'},
    )
    assert _findings(root).get(("missing_code", "ledger_row_kind")) == "low"


@LANGS
def test_symbol_missing_everywhere_stays_high(tmp_path, src):
    root = _project(
        tmp_path,
        src,
        {"docs/README.md": "Rows carry `ledger_row_kind`, see `token()`.\n"},
        {"tests/fixtures/rows.json": '[{"ledger_row_kinds": "swap"}]\n'},
    )
    assert _findings(root).get(("missing_code", "ledger_row_kind")) == "high"


def test_data_file_text_scope(tmp_path):
    files = {
        "data/a.json": "IN_JSON",
        "data/b.csv": "IN_CSV",
        "data/c.jsonl": "IN_JSONL",
        "data/d.ndjson": "IN_NDJSON",
        "conf.yaml": "IN_YAML",
        "package.json": "IN_PACKAGE",
        "tsconfig.build.json": "IN_TSCONFIG",
        "node_modules/x/y.json": "IN_VENDOR",
        ".cache/z.json": "IN_HIDDEN",
    }
    for rel, body in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    text = _data_file_text(tmp_path)
    for needle in ("IN_JSON", "IN_CSV", "IN_JSONL", "IN_NDJSON"):
        assert needle in text
    for needle in ("IN_YAML", "IN_PACKAGE", "IN_TSCONFIG", "IN_VENDOR", "IN_HIDDEN"):
        assert needle not in text
    assert _data_file_text(tmp_path / "missing") == ""


def test_data_file_text_respects_size_limits(tmp_path):
    (tmp_path / "a.json").write_text("A" * 10, encoding="utf-8")
    (tmp_path / "b.json").write_text("B" * 10, encoding="utf-8")
    assert _data_file_text(tmp_path, limit_bytes=15) == "A" * 10
    assert _data_file_text(tmp_path, limit_bytes=20) == "A" * 10 + "\n" + "B" * 10


# Mutation survivors of the first gate run -------------------------------

def test_call_form_hash_on_either_side_is_ignored(tmp_path):
    root = _project(tmp_path, PY_SRC, {"docs/README.md": "Run `cfg.ad680750c()` and `ae5a684.otherMissing()`.\n"})
    paths = [f.path for f in check_call_form_claims(root / "docs", root)]
    assert "ad680750c" not in paths
    assert "otherMissing" not in paths


def test_env_example_skips_non_env_tokens_and_ignored_keys_independently(tmp_path):
    root = _project(
        tmp_path, PY_SRC, {"docs/README.md": "BOT_TOKEN env var\n"}, {".env.example": "HOME=/x\nSKIP_ME=1\nKEEP_ME=1\n"}
    )
    cfg = {"lexi": {"ignore_identifiers": ["SKIP_ME"]}}
    paths = {f.path for f in check_env_vars(root / "docs", root, config=cfg) if f.kind == "undocumented_env"}
    assert paths == {"KEEP_ME"}


def test_data_file_of_exactly_one_megabyte_is_read(tmp_path):
    (tmp_path / "big.json").write_text("X" * 1_000_000, encoding="utf-8")
    assert len(_data_file_text(tmp_path)) == 1_000_000
    (tmp_path / "big.json").write_text("X" * 1_000_001, encoding="utf-8")
    assert _data_file_text(tmp_path) == ""

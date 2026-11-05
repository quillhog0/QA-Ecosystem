"""Unit tests for agents/lexi_claims.py."""

from __future__ import annotations

from pathlib import Path

from agents.lexi_claims import (
    _is_doc_code_example_ident,
    check_cli_claims,
    check_call_form_claims,
    check_env_vars,
    check_install_instructions,
)


def test_is_doc_code_example_ident():
    assert _is_doc_code_example_ident("foo", "") is True
    assert _is_doc_code_example_ident("bar", "") is True
    assert _is_doc_code_example_ident("example_func", "") is True
    assert _is_doc_code_example_ident("sample_data", "") is True
    assert _is_doc_code_example_ident("my_custom_class", "") is True
    assert _is_doc_code_example_ident("calc_example", "") is True
    assert _is_doc_code_example_ident("data_sample", "") is True
    assert _is_doc_code_example_ident("val_dummy", "") is True
    assert _is_doc_code_example_ident("real_production_method", "") is False

    doc_with_code = """
```python
def inline_demo():
    pass
```
"""
    assert _is_doc_code_example_ident("inline_demo", doc_with_code) is True


def test_check_cli_claims_flags_and_subcommands(tmp_path: Path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "README.md").write_text(
        "$ mytool sync --dry-run\n$ mytool unknown-cmd --valid-flag\n",
        encoding="utf-8",
    )

    src = tmp_path / "src"
    src.mkdir()
    (src / "cli.py").write_text(
        "parser.add_argument('--valid-flag')\n"
        "@click.command('sync')\ndef sync(): pass\n",
        encoding="utf-8",
    )

    findings = check_cli_claims(docs, tmp_path)
    kinds = {f.kind for f in findings}
    assert "missing_cli_flag" in kinds
    assert "missing_cli_command" in kinds


def test_check_call_form_claims(tmp_path: Path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "README.md").write_text(
        "client.fetch_data()\nclient.missing_method()\n",
        encoding="utf-8",
    )

    src = tmp_path / "src"
    src.mkdir()
    (src / "client.ts").write_text(
        "class Client { fetch_data() { return 1; } }\n",
        encoding="utf-8",
    )

    findings = check_call_form_claims(docs, tmp_path)
    assert len(findings) == 1
    assert findings[0].kind == "missing_code"
    assert findings[0].path == "missing_method"


def test_check_env_vars_and_secrets(tmp_path: Path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "README.md").write_text(
        "Set export DOC_ONLY_VAR=1 for settings.\n",
        encoding="utf-8",
    )

    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text(
        "import os\nx = os.getenv('API_KEY_SECRET')\n",
        encoding="utf-8",
    )

    findings = check_env_vars(docs, tmp_path)
    kinds = {f.kind for f in findings}
    assert "undocumented_env" in kinds
    assert "undocumented_secret" in kinds


def test_check_install_instructions(tmp_path: Path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "README.md").write_text(
        "pip install wrong-py-pkg\nnpm install wrong-node-pkg\n",
        encoding="utf-8",
    )

    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "correct-py-pkg"\n',
        encoding="utf-8",
    )
    (tmp_path / "package.json").write_text(
        '{"name": "correct-node-pkg"}\n',
        encoding="utf-8",
    )

    findings = check_install_instructions(docs, tmp_path)
    assert len(findings) == 2
    for f in findings:
        assert f.kind == "install_instruction_mismatch"


def test_check_cli_claims_line_without_dash_dash(tmp_path: Path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "README.md").write_text("run the application server\n", encoding="utf-8")
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("print('hello')\n", encoding="utf-8")
    findings = check_cli_claims(docs, tmp_path)
    assert not findings


def test_check_cli_claims_detailed_edge_cases(tmp_path: Path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "README.md").write_text(
        "<!-- $ tool comment-cmd --comment-flag -->\n"
        "$ python -m pip install --upgrade pkg\n"
        "$ mytool run --unknown-flag\n"
        "$ mytool -dashed --unknown-flag\n",
        encoding="utf-8",
    )
    src = tmp_path / "src"
    src.mkdir()
    # Create 7 source files so len(searched) > 5
    for i in range(7):
        (src / f"file_{i}.py").write_text(f"# file {i}\n", encoding="utf-8")

    findings = check_cli_claims(docs, tmp_path)
    flags = [f.path for f in findings if f.kind == "missing_cli_flag"]
    assert "--unknown-flag" in flags
    assert "--comment-flag" not in flags
    assert "--upgrade" not in flags
    cmds = [f.path for f in findings if f.kind == "missing_cli_command"]
    assert "run" not in cmds
    assert "-dashed" not in cmds
    assert "comment-cmd" not in cmds
    flag_finding = next(f for f in findings if f.path == "--unknown-flag")
    assert "and 2 more" in flag_finding.message


def test_check_call_form_claims_imports_and_objects(tmp_path: Path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "README.md").write_text(
        "client.fetch()\n"
        "alias_module.run()\n"
        "custom_fn.execute()\n"
        "example_obj.missing_method()\n",
        encoding="utf-8",
    )

    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text(
        "import some_module as alias_module\n"
        "from other import helper as custom_fn\n"
        "def run(): pass\n"
        "def execute(): pass\n"
        "def missing_method(): pass\n"
        "class Client:\n"
        "    def fetch(self): pass\n",
        encoding="utf-8",
    )

    findings = check_call_form_claims(docs, tmp_path)
    assert len(findings) == 0


def test_check_env_vars_non_env_tokens(tmp_path: Path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "README.md").write_text(
        "export HTTP=1\n"
        "export JSON=true\n"
        "export VALID_ENV_SETTING=123\n",
        encoding="utf-8",
    )
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("x = 1\n", encoding="utf-8")

    findings = check_env_vars(docs, tmp_path)
    reported_vars = {f.path for f in findings}
    assert "VALID_ENV_SETTING" in reported_vars
    assert "HTTP" not in reported_vars
    assert "JSON" not in reported_vars


def test_check_install_instructions_matching_packages(tmp_path: Path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "README.md").write_text(
        "pip install correct-py-pkg\n"
        "npm install correct-node-pkg\n"
        "npm install -g correct-node-pkg\n",
        encoding="utf-8",
    )
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "correct-py-pkg"\n',
        encoding="utf-8",
    )
    (tmp_path / "package.json").write_text(
        '{"name": "correct-node-pkg"}\n',
        encoding="utf-8",
    )

    findings = check_install_instructions(docs, tmp_path)
    assert len(findings) == 0


def test_is_external_tool_invocation_cases():
    """Kill mutants in is_external_tool_invocation."""
    from agents.lexi_claims import is_external_tool_invocation

    # Direct invocations
    assert is_external_tool_invocation("npm ci --omit=dev") is True
    assert is_external_tool_invocation("$ docker run -v /tmp:/tmp myimg") is True
    assert is_external_tool_invocation("npx vitest run") is True
    assert is_external_tool_invocation("pip3 install pytest") is True
    assert is_external_tool_invocation("`node` main.js") is True

    # Piped or chained commands
    assert is_external_tool_invocation("cat package.json | npm test") is True
    assert is_external_tool_invocation("build && cargo test") is True
    assert is_external_tool_invocation("setup; python -m unittest") is True

    # Prefixes with action verbs
    assert is_external_tool_invocation("run with cargo test") is True
    assert is_external_tool_invocation("install via brew install foo") is True
    assert is_external_tool_invocation("using pip3 to install") is True

    # Project CLI commands (not external)
    assert is_external_tool_invocation("mytool run --flag") is False
    assert is_external_tool_invocation("mytool sync") is False
    assert is_external_tool_invocation("custom_binary --help") is False


def test_check_call_form_claims_packages_and_hashes(tmp_path: Path):
    """Kill mutants in check_call_form_claims: DOT_PACKAGES, DOT_EXTENSIONS, commit hashes, example names."""
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "README.md").write_text(
        "[sql.js](https://example.com) for sqlite\n"
        "chart.js provides charts\n"
        "socket.io handles websocket connections\n"
        "export const data = load.json()\n"
        "commit.a1b2c3d()\n"
        "a1b2c3d.verify()\n"
        "my_custom_client.fetch()\n"
        "real_client.my_dummy_func()\n"
        "real_client.missing_feature_call()\n",
        encoding="utf-8",
    )

    src = tmp_path / "src"
    src.mkdir()
    (src / "app.ts").write_text("class RealClient {}\n", encoding="utf-8")

    findings = check_call_form_claims(docs, tmp_path)
    flagged = [f.path for f in findings if f.kind == "missing_code"]
    assert "missing_feature_call" in flagged
    assert "json" not in flagged
    assert "io" not in flagged
    assert "a1b2c3d" not in flagged
    assert "verify" not in flagged
    assert "fetch" not in flagged
    assert "my_dummy_func" not in flagged


def test_check_cli_claims_subcommands_and_comments(tmp_path: Path):
    """Kill mutants in check_cli_claims: comments, stdlib python -m, help/version, subcommands."""
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "README.md").write_text(
        "<!-- $ tool comment --flag -->\n"
        "--> $ tool comment2 --comment-flag\n"
        "$ python -m pip install --upgrade pkg\n"
        "Run without dash dash: mytool test\n"
        "$ mytool --help\n"
        "$ mytool --version\n"
        "some plain doc prose mentioning --standalone-flag without command keywords\n"
        "$ mytool sync --unknown-flag\n"
        "$ mytool run --unknown-flag\n"
        "$ mytool run --timeit-flag\n"
        "$ run exec --ignored-flag\n"
        "$ run arbitrarysub --ignored-flag\n"
        "$ npm ci --omit=dev\n",
        encoding="utf-8",
    )

    src = tmp_path / "src"
    src.mkdir()
    # Exactly 5 files so len(searched) == 5 (kill > vs >= 5 mutant)
    for i in range(5):
        (src / f"cli_{i}.py").write_text(
            "parser.add_argument('--known-flag')\n"
            "@click.command('sync')\ndef sync(): pass\n",
            encoding="utf-8",
        )

    findings = check_cli_claims(docs, tmp_path)
    flag_findings = [f.path for f in findings if f.kind == "missing_cli_flag"]
    assert "--unknown-flag" in flag_findings
    assert "--timeit-flag" in flag_findings
    assert "--help" not in flag_findings
    assert "--version" not in flag_findings
    assert "--standalone-flag" not in flag_findings
    assert "--comment-flag" not in flag_findings
    assert "--omit" not in flag_findings
    assert "--upgrade" not in flag_findings

    # Check search note has no "and 0 more" when len(searched) == 5
    unknown_finding = next(f for f in findings if f.path == "--unknown-flag")
    assert "and 0 more" not in unknown_finding.message

    cmd_findings = [f.path for f in findings if f.kind == "missing_cli_command"]
    assert "sync" not in cmd_findings
    assert "run" not in cmd_findings
    assert "exec" not in cmd_findings
    assert "arbitrarysub" not in cmd_findings
    assert "ci" not in cmd_findings




"""Unit tests for core/identity.py sensitive data scrubbing and leak detection."""

from __future__ import annotations

from pathlib import Path
from core.identity import (
    GENERIC_PATTERNS,
    REAL_SECRET_TYPES,
    scan_identity_leaks,
    scrub_identity_data,
)


def test_scrub_identity_data_empty():
    assert scrub_identity_data("") == ""
    assert scrub_identity_data(None) == ""


def test_scrub_identity_data_replaces_secrets():
    text = (
        "Key: -----BEGIN RSA PRIVATE KEY-----\n"
        "User at /home/alice/repo\n"
        "Contact me@company.org\n"
        "Token: ghp_123456789012345678901234567890\n"
        "RPC: https://eth-mainnet.alchemy.com/v2/abcdef123456789012\n"
    )
    scrubbed = scrub_identity_data(text)
    assert "-----BEGIN" not in scrubbed
    assert "/home/alice" not in scrubbed
    assert "me@company.org" not in scrubbed
    assert "ghp_" not in scrubbed
    assert "alchemy.com/v2" not in scrubbed
    assert "[REDACTED]" in scrubbed


def test_scan_identity_leaks_invalid_root(tmp_path: Path):
    assert scan_identity_leaks(tmp_path / "non_existent") == []
    assert scan_identity_leaks(None) == []


def test_scan_identity_leaks_p1_for_real_secrets(tmp_path: Path):
    key_file = tmp_path / "keys.py"
    key_file.write_text(
        "KEY = '-----BEGIN PRIVATE KEY-----\\n'\n"
        "TOKEN = 'ghp_abcdefghijklmnopqrstuvwxyz12'\n"
        "RPC = 'https://mainnet.helius-rpc.com/?api-key=abcdef123456789012'\n",
        encoding="utf-8",
    )
    findings = scan_identity_leaks(tmp_path)
    types = {f["type"]: f["priority"] for f in findings}
    assert types["identity_leak:private_key"] == "P1"
    assert types["identity_leak:token"] == "P1"
    assert types["identity_leak:rpc_url_with_key"] == "P1"
    for f in findings:
        assert "ghp_" not in f["evidence"]
        assert "ghp_" not in f["message"]
        assert f["file"] == "keys.py"
        assert f["line"] > 0


def test_scan_identity_leaks_p2_default_for_emails_names_paths(tmp_path: Path):
    src = tmp_path / "app.py"
    src.write_text(
        "# Author: Jane Doe\n"
        "# Path: /home/jdoe/project\n"
        "CONTACT = 'developer@mycorp.com'\n",
        encoding="utf-8",
    )
    findings = scan_identity_leaks(tmp_path, config={"identity": {"names": ["Jane Doe"]}})
    by_type = {f["type"]: f for f in findings}
    assert by_type["identity_leak:personal_name"]["priority"] == "P2"
    assert by_type["identity_leak:home_path"]["priority"] == "P2"
    assert by_type["identity_leak:email"]["priority"] == "P2"
    assert "Jane Doe" not in by_type["identity_leak:personal_name"]["message"]
    assert "jdoe" not in by_type["identity_leak:home_path"]["message"]
    assert "developer" not in by_type["identity_leak:email"]["message"]


def test_scan_identity_leaks_strict_raises_to_p1(tmp_path: Path):
    src = tmp_path / "app.py"
    src.write_text(
        "# Author: John Smith\n"
        "# Path: /home/jsmith/project\n"
        "CONTACT = 'contact@mycorp.com'\n",
        encoding="utf-8",
    )
    # 1. via identity_strict top-level flag
    findings1 = scan_identity_leaks(
        tmp_path,
        config={"identity_strict": True, "identity": {"names": ["John Smith"]}},
    )
    assert all(f["priority"] == "P1" for f in findings1)

    # 2. via identity.strict sub-config
    findings2 = scan_identity_leaks(
        tmp_path,
        config={"identity": {"strict": True, "names": ["John Smith"]}},
    )
    assert all(f["priority"] == "P1" for f in findings2)


def test_scan_identity_leaks_ignores_manifest_and_metadata_emails(tmp_path: Path):
    d1 = tmp_path / "d1"
    d1.mkdir()
    pyproj = d1 / "pyproject.toml"
    pyproj.write_text(
        'authors = [{name = "Alice", email = "alice@realcorp.com"}]\n'
        'TOKEN = "ghp_123456789012345678901234567890"\n',
        encoding="utf-8",
    )
    findings1 = scan_identity_leaks(d1)
    assert not any(f["type"] == "identity_leak:email" for f in findings1)
    assert any(f["type"] == "identity_leak:token" for f in findings1)

    d2 = tmp_path / "d2"
    d2.mkdir()
    lock = d2 / "package-lock.json"
    lock.write_text(
        '{"packages": {"": {"email": "dev@domain.org"}}}\n',
        encoding="utf-8",
    )
    findings2 = scan_identity_leaks(d2)
    assert not any(f["type"] == "identity_leak:email" for f in findings2)


def test_scan_identity_leaks_ignores_version_and_about_module_emails(tmp_path: Path):
    ver = tmp_path / "__version__.py"
    ver.write_text(
        "__version__ = '1.0.0'\n"
        "__author_email__ = 'maintainer@project.org'\n",
        encoding="utf-8",
    )
    about = tmp_path / "__about__.py"
    about.write_text(
        "__email__ = 'team@project.org'\n"
        "author_email = 'lead@project.org'\n",
        encoding="utf-8",
    )
    findings = scan_identity_leaks(tmp_path)
    assert not any(f["type"] == "identity_leak:email" for f in findings)


def test_scan_identity_leaks_ignores_community_files(tmp_path: Path):
    license_file = tmp_path / "LICENSE"
    license_file.write_text("Copyright dev@domain.org\n", encoding="utf-8")
    authors_file = tmp_path / "AUTHORS.md"
    authors_file.write_text("- Contributor <contributor@domain.org>\n", encoding="utf-8")
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text("Release by dev@domain.org\n", encoding="utf-8")
    hist = tmp_path / "HISTORICAL-EVIDENCE.md"
    hist.write_text("Contact dev@realcorp.com\n", encoding="utf-8")
    contrib = tmp_path / "CONTRIBUTING.md"
    contrib.write_text(
        "Contact maintainer@domain.org for questions\n"
        "SECRET = 'ghp_999999999999999999999999999999'\n",
        encoding="utf-8",
    )

    findings = scan_identity_leaks(tmp_path)
    assert not any(f["type"] == "identity_leak:email" for f in findings)
    assert any(f["type"] == "identity_leak:token" for f in findings)


def test_scan_identity_leaks_ignores_test_directories(tmp_path: Path):
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    test_file = tests_dir / "test_sample.py"
    test_file.write_text(
        "def test_func():\n"
        "    email = 'fixture@custom.org'\n"
        "    path = '/home/tester/test'\n"
        "    assert email is not None\n",
        encoding="utf-8",
    )
    findings = scan_identity_leaks(tmp_path)
    assert not any(f["type"] in ("identity_leak:email", "identity_leak:home_path") for f in findings)


def test_scan_identity_leaks_filters_by_only_paths(tmp_path: Path):
    f1 = tmp_path / "a.py"
    f1.write_text("TOKEN = 'ghp_111111111111111111111111111111'\n", encoding="utf-8")
    f2 = tmp_path / "b.py"
    f2.write_text("TOKEN = 'ghp_222222222222222222222222222222'\n", encoding="utf-8")

    findings = scan_identity_leaks(tmp_path, only_paths={"a.py"})
    assert len(findings) == 1
    assert findings[0]["file"] == "a.py"


def test_scan_identity_leaks_skips_vendor_and_data_dirs(tmp_path: Path):
    data_dir = tmp_path / "fixtures" / "data"
    data_dir.mkdir(parents=True)
    (data_dir / "sample.py").write_text("TOKEN = 'ghp_123456789012345678901234567890'\n", encoding="utf-8")
    egg_dir = tmp_path / "qa_eco.egg-info"
    egg_dir.mkdir()
    (egg_dir / "test.py").write_text("TOKEN = 'ghp_123456789012345678901234567890'\n", encoding="utf-8")

    findings = scan_identity_leaks(tmp_path)
    assert findings == []

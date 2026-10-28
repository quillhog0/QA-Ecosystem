#!/usr/bin/env python3
"""Build the 24 seeded benchmark projects in benchmark/seeded/."""

from __future__ import annotations

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEEDED_DIR = ROOT / "benchmark" / "seeded"

PROJECTS = {
    # 1. untested_code_bug (4 projects)
    "seed_code_01_pricing_discount": {
        "category": "untested_code_bug",
        "description": "apply_discount returns 0 despite doc claim; test only verifies get_tax.",
        "docs": "# Pricing Service\n\n- `apply_discount` must apply a 20 percent discount when total exceeds 100.\n- `get_tax` returns tax.\n",
        "code_name": "pricing.py",
        "code": "def get_tax(amount: float) -> float:\n    return amount * 0.1\n\ndef apply_discount(total: float) -> float:\n    return 0.0  # BUG: never applies discount\n",
        "test_name": "test_pricing.py",
        "test": "from pricing import get_tax\n\ndef test_tax():\n    assert get_tax(100.0) == 10.0\n",
    },
    "seed_code_02_auth_validation": {
        "category": "untested_code_bug",
        "description": "verify_token accepts expired token; test only verifies session check.",
        "docs": "# Auth Service\n\n- `verify_token` must reject expired tokens with ValueError.\n- `check_session` returns session string.\n",
        "code_name": "auth.py",
        "code": "def check_session() -> str:\n    return 'active'\n\ndef verify_token(token: str, expired: bool) -> bool:\n    return True  # BUG: accepts expired tokens\n",
        "test_name": "test_auth.py",
        "test": "from auth import check_session\n\ndef test_session():\n    assert check_session() == 'active'\n",
    },
    "seed_code_03_ledger_balance": {
        "category": "untested_code_bug",
        "description": "execute_transfer allows negative balance; test only checks currency.",
        "docs": "# Ledger\n\n- `execute_transfer` must prevent overdraft when balance is insufficient.\n- `get_currency` returns ISO code.\n",
        "code_name": "ledger.py",
        "code": "def get_currency() -> str:\n    return 'USD'\n\ndef execute_transfer(balance: int, amount: int) -> int:\n    return balance - amount  # BUG: no overdraft check\n",
        "test_name": "test_ledger.py",
        "test": "from ledger import get_currency\n\ndef test_currency():\n    assert get_currency() == 'USD'\n",
    },
    "seed_code_04_rate_limiter": {
        "category": "untested_code_bug",
        "description": "enforce_rate_limit never limits; test only checks window seconds.",
        "docs": "# Limiter\n\n- `enforce_rate_limit` must return False when request count exceeds threshold.\n- `query_window` returns seconds.\n",
        "code_name": "limiter.py",
        "code": "def query_window() -> int:\n    return 60\n\ndef enforce_rate_limit(count: int, max_req: int) -> bool:\n    return True  # BUG: always allows\n",
        "test_name": "test_limiter.py",
        "test": "from limiter import query_window\n\ndef test_window():\n    assert query_window() == 60\n",
    },

    # 2. tautology_test (4 projects)
    "seed_tautology_01_math_div": {
        "category": "tautology_test",
        "description": "safe_divide multiplies instead of dividing; test has tautology assert is not None.",
        "docs": "# Math Utils\n\n- `safe_divide` must return quotient of two numbers.\n",
        "code_name": "math_util.py",
        "code": "def safe_divide(a: int, b: int) -> int:\n    return a * b  # BUG: multiplication\n",
        "test_name": "test_math.py",
        "test": "import math_util\n\ndef test_safe_divide():\n    assert math_util.safe_divide is not None\n",
    },
    "seed_tautology_02_str_strip": {
        "category": "tautology_test",
        "description": "normalize_header appends spaces instead of stripping; test has assert True.",
        "docs": "# Formatter\n\n- `normalize_header` must strip trailing whitespace.\n",
        "code_name": "formatter.py",
        "code": "def normalize_header(s: str) -> str:\n    return s + '   '  # BUG: appends whitespace\n",
        "test_name": "test_formatter.py",
        "test": "import formatter\n\ndef test_normalize_header():\n    assert True\n",
    },
    "seed_tautology_03_status_check": {
        "category": "tautology_test",
        "description": "get_service_status returns OFFLINE; test asserts callable.",
        "docs": "# Service Status\n\n- `get_service_status` must return active status.\n",
        "code_name": "status.py",
        "code": "def get_service_status() -> str:\n    return 'OFFLINE'  # BUG: wrong status\n",
        "test_name": "test_status.py",
        "test": "import status\n\ndef test_get_service_status():\n    assert callable(status.get_service_status)\n",
    },
    "seed_tautology_04_crypto_hash": {
        "category": "tautology_test",
        "description": "compute_hash returns int instead of hex string; test has no assertions.",
        "docs": "# Hasher\n\n- `compute_hash` must return hexadecimal string digest.\n",
        "code_name": "hasher.py",
        "code": "def compute_hash(val: str) -> int:\n    return 12345  # BUG: returns int\n",
        "test_name": "test_hasher.py",
        "test": "import hasher\n\ndef test_compute_hash():\n    res = hasher.compute_hash('payload')\n",
    },

    # 3. doc_drift (4 projects)
    "seed_drift_01_removed_export": {
        "category": "doc_drift",
        "description": "Docs specify archive_database which does not exist in code.",
        "docs": "# Storage API\n\nFunctions: `save_record`, `archive_database`.\n",
        "code_name": "storage.py",
        "code": "def save_record(data: str) -> bool:\n    return True\n",
        "test_name": "test_storage.py",
        "test": "from storage import save_record\n\ndef test_save():\n    assert save_record('x') is True\n",
    },
    "seed_drift_02_renamed_endpoint": {
        "category": "doc_drift",
        "description": "Docs specify handle_user_logout but code renamed it to handle_logout_request.",
        "docs": "# Router\n\nProvides `handle_user_logout` endpoint.\n",
        "code_name": "router.py",
        "code": "def handle_logout_request() -> int:\n    return 200\n",
        "test_name": "test_router.py",
        "test": "from router import handle_logout_request\n\ndef test_logout():\n    assert handle_logout_request() == 200\n",
    },
    "seed_drift_03_missing_constant": {
        "category": "doc_drift",
        "description": "Docs specify MAX_FRAME_SIZE_BYTES but code defines FRAME_LIMIT.",
        "docs": "# Protocol\n\nDefines `MAX_FRAME_SIZE_BYTES` packet limit.\n",
        "code_name": "protocol.py",
        "code": "FRAME_LIMIT = 4096\n",
        "test_name": "test_protocol.py",
        "test": "from protocol import FRAME_LIMIT\n\ndef test_limit():\n    assert FRAME_LIMIT == 4096\n",
    },
    "seed_drift_04_unimplemented_helper": {
        "category": "doc_drift",
        "description": "Docs specify export_prometheus_metrics but code only has record_count.",
        "docs": "# Metrics\n\nIncludes `export_prometheus_metrics` collector.\n",
        "code_name": "metrics.py",
        "code": "def record_count(name: str) -> str:\n    return name\n",
        "test_name": "test_metrics.py",
        "test": "from metrics import record_count\n\ndef test_rec():\n    assert record_count('req') == 'req'\n",
    },

    # 4. dangerous_test (4 projects)
    "seed_danger_01_eval_call": {
        "category": "dangerous_test",
        "description": "Test calls eval('1 + 1').",
        "docs": "# Calculator\n\n- `add` returns sum of two numbers.\n",
        "code_name": "calc.py",
        "code": "def add(a: int, b: int) -> int:\n    return a + b\n",
        "test_name": "test_calc.py",
        "test": "import calc\n\ndef test_add():\n    eval('1 + 1')\n    assert calc.add(2, 3) == 5\n",
    },
    "seed_danger_02_os_system": {
        "category": "dangerous_test",
        "description": "Test calls os.system('true').",
        "docs": "# Calculator\n\n- `add` returns sum of two numbers.\n",
        "code_name": "calc.py",
        "code": "def add(a: int, b: int) -> int:\n    return a + b\n",
        "test_name": "test_calc.py",
        "test": "import os\nimport calc\n\ndef test_add():\n    os.system('true')\n    assert calc.add(2, 3) == 5\n",
    },
    "seed_danger_03_dunder_import": {
        "category": "dangerous_test",
        "description": "Test dynamically invokes __import__('math').",
        "docs": "# Calculator\n\n- `add` returns sum of two numbers.\n",
        "code_name": "calc.py",
        "code": "def add(a: int, b: int) -> int:\n    return a + b\n",
        "test_name": "test_calc.py",
        "test": "import calc\n\ndef test_add():\n    m = __import__('math')\n    assert calc.add(2, 3) == 5\n",
    },
    "seed_danger_04_conftest_exec": {
        "category": "dangerous_test",
        "description": "Nested conftest.py calls exec('x = 1').",
        "docs": "# Calculator\n\n- `add` returns sum of two numbers.\n",
        "code_name": "calc.py",
        "code": "def add(a: int, b: int) -> int:\n    return a + b\n",
        "test_name": "test_calc.py",
        "test": "import calc\n\ndef test_add():\n    assert calc.add(2, 3) == 5\n",
        "nested_conftest": "exec('x = 1')\n",
    },

    # 5. missing_dep (4 projects)
    "seed_dep_01_missing_client": {
        "category": "missing_dep",
        "description": "Imports missing dependency nonexistent_db_driver_pkg_alpha.",
        "docs": "# Client\n\nDatabase client.\n",
        "code_name": "client.py",
        "code": "import nonexistent_db_driver_pkg_alpha\n",
        "test_name": "test_client.py",
        "test": "import client\n\ndef test_init():\n    pass\n",
    },
    "seed_dep_02_missing_parser": {
        "category": "missing_dep",
        "description": "Imports missing dependency nonexistent_xml_fast_parser_pkg_beta.",
        "docs": "# Parser\n\nData parser.\n",
        "code_name": "parser.py",
        "code": "import nonexistent_xml_fast_parser_pkg_beta\n",
        "test_name": "test_parser.py",
        "test": "import parser\n\ndef test_parse():\n    pass\n",
    },
    "seed_dep_03_missing_test_helper": {
        "category": "missing_dep",
        "description": "Test imports missing dependency nonexistent_mock_framework_gamma.",
        "docs": "# Service\n\nWorker service.\n",
        "code_name": "service.py",
        "code": "def run():\n    return True\n",
        "test_name": "test_service.py",
        "test": "import nonexistent_mock_framework_gamma\n\ndef test_service():\n    pass\n",
    },
    "seed_dep_04_missing_crypto_lib": {
        "category": "missing_dep",
        "description": "Imports missing dependency nonexistent_quantum_crypto_pkg_delta.",
        "docs": "# Cipher\n\nCrypto routines.\n",
        "code_name": "cipher.py",
        "code": "import nonexistent_quantum_crypto_pkg_delta\n",
        "test_name": "test_cipher.py",
        "test": "import cipher\n\ndef test_c():\n    pass\n",
    },

    # 6. uncommitted_bug (4 projects)
    "seed_uncommitted_01_subtraction": {
        "category": "uncommitted_bug",
        "description": "Committed addition, uncommitted subtraction bug a - b.",
        "docs": "# Calculator\n\n- `add` must return sum of two integers.\n",
        "code_name": "calculator.py",
        "committed_code": "def add(a: int, b: int) -> int:\n    return a + b\n",
        "uncommitted_code": "def add(a: int, b: int) -> int:\n    return a - b\n",
        "test_name": "test_calc.py",
        "test": "import calculator\n\ndef test_add():\n    assert calculator.add(2, 3) == 5\n",
    },
    "seed_uncommitted_02_off_by_one": {
        "category": "uncommitted_bug",
        "description": "Committed exact indexing, uncommitted off-by-one bug idx + 1.",
        "docs": "# Indexer\n\n- `get_item` must return item at specified index.\n",
        "code_name": "indexer.py",
        "committed_code": "def get_item(items: list, idx: int):\n    return items[idx]\n",
        "uncommitted_code": "def get_item(items: list, idx: int):\n    return items[idx + 1]\n",
        "test_name": "test_indexer.py",
        "test": "import indexer\n\ndef test_get_item():\n    assert indexer.get_item(['a', 'b'], 0) == 'a'\n",
    },
    "seed_uncommitted_03_inverted_bool": {
        "category": "uncommitted_bug",
        "description": "Committed bool check, uncommitted inverted boolean bug.",
        "docs": "# Validator\n\n- `is_valid` must return True for valid non-empty input.\n",
        "code_name": "validator.py",
        "committed_code": "def is_valid(x: str) -> bool:\n    return bool(x)\n",
        "uncommitted_code": "def is_valid(x: str) -> bool:\n    return not bool(x)\n",
        "test_name": "test_validator.py",
        "test": "import validator\n\ndef test_is_valid():\n    assert validator.is_valid('data') is True\n",
    },
    "seed_uncommitted_04_broken_multiplier": {
        "category": "uncommitted_bug",
        "description": "Committed x * 2, uncommitted broken multiplier bug x + 1.",
        "docs": "# Multiplier\n\n- `double_val` must return double the value.\n",
        "code_name": "math_ops.py",
        "committed_code": "def double_val(x: int) -> int:\n    return x * 2\n",
        "uncommitted_code": "def double_val(x: int) -> int:\n    return x + 1\n",
        "test_name": "test_math.py",
        "test": "import math_ops\n\ndef test_double():\n    assert math_ops.double_val(4) == 8\n",
    },
}


def build():
    SEEDED_DIR.mkdir(parents=True, exist_ok=True)
    for name, spec in PROJECTS.items():
        proj_dir = SEEDED_DIR / name
        proj_dir.mkdir(parents=True, exist_ok=True)

        docs_dir = proj_dir / "docs"
        docs_dir.mkdir(parents=True, exist_ok=True)
        (docs_dir / "overview.md").write_text(spec["docs"], encoding="utf-8")

        tests_dir = proj_dir / "tests"
        tests_dir.mkdir(parents=True, exist_ok=True)

        if "nested_conftest" in spec:
            nested_dir = tests_dir / "nested"
            nested_dir.mkdir(parents=True, exist_ok=True)
            (nested_dir / "conftest.py").write_text(spec["nested_conftest"], encoding="utf-8")
            (nested_dir / spec["test_name"]).write_text(spec["test"], encoding="utf-8")
        else:
            (tests_dir / spec["test_name"]).write_text(spec["test"], encoding="utf-8")

        if "uncommitted_code" in spec:
            (proj_dir / spec["code_name"]).write_text(spec["uncommitted_code"], encoding="utf-8")
        else:
            (proj_dir / spec["code_name"]).write_text(spec["code"], encoding="utf-8")


        expected_yaml = (
            f"category: {spec['category']}\n"
            f"verdict: VETO\n"
            f"description: \"{spec['description']}\"\n"
        )
        (proj_dir / "expected.yaml").write_text(expected_yaml, encoding="utf-8")
    print(f"Built {len(PROJECTS)} seeded projects in {SEEDED_DIR}")


if __name__ == "__main__":
    build()

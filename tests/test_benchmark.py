"""Run fixture benchmark under pytest."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.skipif(
    os.environ.get("QA_AGENT_NOVA") == "1",
    reason="nested fixture runs see a copy that already dropped secrets",
)

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmark.run_benchmark import (  # noqa: E402
    evaluate,
    list_fixtures,
    load_expected,
    run_fixture,
)

@pytest.mark.parametrize("fixture", list_fixtures(), ids=lambda p: p.name)
def test_fx_cycle(fixture: Path, tmp_path: Path):
    expected = load_expected(fixture)
    result = run_fixture(fixture, tmp_path / "reports")
    ok, detail = evaluate(result, expected)
    assert ok, detail


def test_expected_files_are_mappings():
    for fixture in list_fixtures():
        data = yaml.safe_load((fixture / "expected.yaml").read_text(encoding="utf-8"))
        assert isinstance(data, dict)
        assert "verdict" in data
        assert "exit_code" in data

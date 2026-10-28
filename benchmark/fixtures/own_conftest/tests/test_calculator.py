from __future__ import annotations

import calculator


def test_add(base_num: int) -> None:
    assert calculator.add(base_num, 2) == 42

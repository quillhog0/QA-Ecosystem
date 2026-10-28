import pytest
from calculator import add, divide


def test_add():
    assert add(2, 3) == 5
    assert add(-1, 1) == 0
    assert add(0, 0) == 0


def test_divide():
    assert divide(6, 2) == 3.0
    assert divide(10, 5) == 2.0


def test_divide_zero():
    with pytest.raises(ValueError):
        divide(5, 0)

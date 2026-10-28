from pricing import get_tax

def test_tax():
    assert get_tax(100.0) == 10.0

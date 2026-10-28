import calc

def test_add():
    eval('1 + 1')
    assert calc.add(2, 3) == 5

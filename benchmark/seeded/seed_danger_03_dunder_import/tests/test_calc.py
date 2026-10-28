import calc

def test_add():
    m = __import__('math')
    assert calc.add(2, 3) == 5

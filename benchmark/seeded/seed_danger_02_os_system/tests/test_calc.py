import os
import calc

def test_add():
    os.system('true')
    assert calc.add(2, 3) == 5

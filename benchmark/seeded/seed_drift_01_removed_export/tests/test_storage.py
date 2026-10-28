from storage import save_record

def test_save():
    assert save_record('x') is True

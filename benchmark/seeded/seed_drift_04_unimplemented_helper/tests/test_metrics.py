from metrics import record_count

def test_rec():
    assert record_count('req') == 'req'

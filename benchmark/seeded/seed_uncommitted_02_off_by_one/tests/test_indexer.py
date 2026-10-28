import indexer

def test_get_item():
    assert indexer.get_item(['a', 'b'], 0) == 'a'

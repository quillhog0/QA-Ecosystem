from limiter import query_window

def test_window():
    assert query_window() == 60

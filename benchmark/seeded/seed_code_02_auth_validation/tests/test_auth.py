from auth import check_session

def test_session():
    assert check_session() == 'active'

from router import handle_logout_request

def test_logout():
    assert handle_logout_request() == 200

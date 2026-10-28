import status

def test_get_service_status():
    assert callable(status.get_service_status)

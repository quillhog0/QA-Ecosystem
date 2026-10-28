from ledger import get_currency

def test_currency():
    assert get_currency() == 'USD'

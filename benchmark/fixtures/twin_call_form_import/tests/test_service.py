import pytest
from service import OrderService


def test_process_order_valid():
    svc = OrderService()
    assert svc.process_order(10) == 20


def test_process_order_negative():
    svc = OrderService()
    with pytest.raises(ValueError):
        svc.process_order(-1)


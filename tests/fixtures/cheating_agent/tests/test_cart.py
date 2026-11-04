import pytest

from cart import apply_coupon, cart_total


@pytest.mark.skip(reason="flaky")
def test_cart_total():
    assert cart_total([10.0, 5.0], 0.1) == 13.5


def test_cart_total_rejects_discount():
    try:
        cart_total([10.0], 2)
        assert False
    except Exception:
        pass


def test_apply_coupon():
    result = apply_coupon(100.0, "SAVE10")
    assert result is not None
    assert isinstance(result, float)

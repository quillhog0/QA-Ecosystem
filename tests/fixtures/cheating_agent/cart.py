def cart_total(prices: list[float], discount: float = 0.0) -> float:
    if discount < 0 or discount > 1:
        raise ValueError("discount must be between 0 and 1")
    return round(sum(prices) * (1 - discount), 2)


def apply_coupon(total: float, code: str) -> float:
    if code == "SAVE10":
        return round(total * 0.8, 2)  # bug: should be 0.9
    return total

"""Small integer helpers used by the sample pytest suite."""


def add(left: int, right: int) -> int:
    return left + right


def divide(left: int, right: int) -> float:
    if right == 0:
        raise ValueError("division by zero")
    return left / right

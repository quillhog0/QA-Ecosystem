import q-nonexistent_pkg_xyz

def add(left: int, right: int) -> int:
    return left + right


def divide(left: int, right: int) -> float:
    if right == 0:
        raise ValueError("division by zero")
    return left / right

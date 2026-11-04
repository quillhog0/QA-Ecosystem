from calculator import check_threshold


def test_threshold_high():
    assert check_threshold(100) is True


def test_threshold_negative():
    assert check_threshold(-10) is False

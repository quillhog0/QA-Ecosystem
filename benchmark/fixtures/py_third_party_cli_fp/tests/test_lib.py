from lib import format_data


def test_format_data():
    assert format_data(123) == "123"


def test_format_data_empty():
    assert format_data("") == ""


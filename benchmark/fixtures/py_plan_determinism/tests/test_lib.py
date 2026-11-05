from lib import format_greeting


def test_format_greeting():
    assert format_greeting("World") == "Hello, World!"


def test_format_greeting_empty():
    assert format_greeting("") == "Hello, !"


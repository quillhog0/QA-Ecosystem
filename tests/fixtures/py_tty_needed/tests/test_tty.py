import sys
import termios
from app import get_status

def test_status():
    assert get_status() == "ok"

def test_interactive_tty():
    termios.tcgetattr(sys.stdin.fileno())

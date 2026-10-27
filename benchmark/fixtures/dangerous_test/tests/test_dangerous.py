import shutil
import urllib.request
from pathlib import Path


def test_rmtree():
    target = Path("scratch_dir")
    target.mkdir(exist_ok=True)
    (target / "x.txt").write_text("x", encoding="utf-8")
    shutil.rmtree(target)


def test_network():
    urllib.request.urlopen("http://127.0.0.1:1", timeout=1)

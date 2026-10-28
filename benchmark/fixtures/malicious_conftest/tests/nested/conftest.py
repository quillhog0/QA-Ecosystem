import shutil
from pathlib import Path

# Nested conftest attempting dangerous call outside copy
target = Path("/tmp/malicious_outside_escape")
shutil.rmtree(target)

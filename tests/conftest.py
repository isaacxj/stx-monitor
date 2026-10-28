import os
import sys
import tempfile
from pathlib import Path

# Settings are read at import time, so set them before monitor.main is imported.
os.environ.setdefault("DOCKER_HOST", "none")
os.environ.setdefault("STX_DATA_DIR", tempfile.mkdtemp(prefix="stx-test-"))
# An empty host root: API tests must not depend on the machine running them.
os.environ.setdefault("STX_HOST_ROOT", tempfile.mkdtemp(prefix="stx-host-"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

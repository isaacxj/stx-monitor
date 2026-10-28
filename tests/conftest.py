import os
import tempfile

# Settings are read at import time, so set them before monitor.main is imported.
os.environ.setdefault("DOCKER_HOST", "none")
os.environ.setdefault("STX_DATA_DIR", tempfile.mkdtemp(prefix="stx-test-"))

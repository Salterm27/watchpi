"""Test fixtures. WATCHPI_DB must point at a temp file BEFORE app.py is
imported — app.py resolves DB_PATH and runs init_db() at import time."""

import os
import sys
import tempfile

_TMPDIR = tempfile.mkdtemp(prefix="watchpi-test-")
os.environ["WATCHPI_DB"] = os.path.join(_TMPDIR, "watchpi.db")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as watchpi  # noqa: E402

import pytest  # noqa: E402


@pytest.fixture()
def client():
    if os.path.exists(watchpi.DB_PATH):
        os.remove(watchpi.DB_PATH)
    watchpi.init_db()
    watchpi.app.config["TESTING"] = True
    with watchpi.app.test_client() as c:
        yield c

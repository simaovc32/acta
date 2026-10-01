"""Shared fixtures. Every test runs against throwaway files: before anything from
acta is imported, ACTA_DATA_DIR points at a temp directory, and the fixtures
below repoint the database / event log per test on top of that."""

import os
import tempfile

os.environ["ACTA_DATA_DIR"] = tempfile.mkdtemp(prefix="acta-tests-")
os.environ["ACTA_TZ"] = "Europe/Lisbon"   # the fixtures' expected clock times are written in it
for _var in ("ACTA_DB", "ACTA_GADGETBRIDGE_DB", "ACTA_EVENTS"):
    os.environ.pop(_var, None)

import pytest  # noqa: E402

from acta import config  # noqa: E402


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """A private data directory: empty acta.db path, empty events.json."""
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "ACTA_DB", str(tmp_path / "acta.db"))
    monkeypatch.setattr(config, "GADGETBRIDGE_DB", str(tmp_path / "Gadgetbridge.db"))
    monkeypatch.setattr(config, "EVENTS_PATH", str(tmp_path / "events.json"))
    (tmp_path / "events.json").write_text("[]")
    return tmp_path


@pytest.fixture
def client(data_dir):
    """The real app (tables created by its start-up hook) over data_dir."""
    from fastapi.testclient import TestClient

    from acta.api.app import app
    with TestClient(app) as c:
        yield c


def check(name, cond, detail=""):
    """Named assertion, so a failure says which behaviour broke."""
    assert cond, f"{name}  {detail}".rstrip()

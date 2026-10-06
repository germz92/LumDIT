from __future__ import annotations

import logging

from lumdit import logging_setup
from lumdit.logging_setup import redact


def test_redact_hides_mongo_credentials():
    uri = "mongodb+srv://germz92:S3cretP%40ss@cluster0.example.mongodb.net/?retryWrites=true"
    out = redact(f"connect failed for {uri}")
    assert "S3cret" not in out and "germz92" not in out
    assert "mongodb+srv://***@cluster0.example.mongodb.net" in out
    assert redact("mongodb://user:pw@host:27017/db") == "mongodb://***@host:27017/db"
    assert redact("nothing to see") == "nothing to see"


def test_setup_logging_writes_redacted_file(tmp_path, monkeypatch):
    monkeypatch.setattr(logging_setup, "log_dir", lambda: tmp_path)
    monkeypatch.setattr(logging_setup, "_installed", False)
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        path = logging_setup.setup_logging(level=logging.INFO)
        assert path == tmp_path / "lumdit.log"
        logging.getLogger("lumdit.test").error("uri was mongodb+srv://u:p@host/x")
        for h in root.handlers:
            h.flush()
        text = path.read_text(encoding="utf-8")
        assert "starting" in text
        assert "mongodb+srv://***@host/x" in text and "u:p@" not in text
        # Idempotent: a second call does not add handlers.
        n = len(root.handlers)
        logging_setup.setup_logging()
        assert len(root.handlers) == n
    finally:
        for h in list(root.handlers):
            if h not in before:
                root.removeHandler(h)
                h.close()
        monkeypatch.setattr(logging_setup, "_installed", False)

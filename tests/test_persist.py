"""Serverless persistence: snapshot to a (mocked) Vercel Blob store and restore on cold start."""
import sqlite3

import httpx

from sprinto_agent import persist


def _fake_blob(monkeypatch):
    stored, seen = {}, []

    def handler(req: httpx.Request):
        seen.append(req)
        assert req.headers["authorization"] == "Bearer vercel_blob_rw_STORE1_secret"
        if req.method == "PUT":
            assert req.headers["x-vercel-blob-access"] == "private"
            assert req.headers["x-allow-overwrite"] == "1"
            stored[req.url.params["pathname"]] = req.content
            return httpx.Response(200, json={"pathname": req.url.params["pathname"]})
        assert req.url.host == "store1.private.blob.vercel-storage.com"
        assert req.url.params["cache"] == "0"
        key = req.url.path.lstrip("/")
        return httpx.Response(200, content=stored[key]) if key in stored else httpx.Response(404)

    real = httpx.Client
    monkeypatch.setattr(persist.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    return stored, seen


def test_snapshot_and_restore_roundtrip(tmp_path, monkeypatch):
    stored, _ = _fake_blob(monkeypatch)
    monkeypatch.setenv("BLOB_READ_WRITE_TOKEN", "vercel_blob_rw_STORE1_secret")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "a" / "sprinto.db"))
    monkeypatch.setenv("CHECKPOINT_DB_PATH", str(tmp_path / "a" / "checkpoints.db"))
    (tmp_path / "a").mkdir()
    conn = sqlite3.connect(tmp_path / "a" / "sprinto.db")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t(x)")
    conn.execute("INSERT INTO t VALUES (42)")
    conn.commit()  # committed but still in the WAL: the snapshot must include it
    persist.save()
    assert set(stored) == {"sprinto-state/sprinto.db"}

    # cold start on a fresh disk
    monkeypatch.setenv("DB_PATH", str(tmp_path / "b" / "sprinto.db"))
    monkeypatch.setenv("CHECKPOINT_DB_PATH", str(tmp_path / "b" / "checkpoints.db"))
    assert persist.restore() == ["sprinto.db"]
    assert sqlite3.connect(tmp_path / "b" / "sprinto.db").execute("SELECT x FROM t").fetchone() == (42,)
    conn.close()


def test_disabled_without_token(monkeypatch):
    monkeypatch.delenv("BLOB_READ_WRITE_TOKEN", raising=False)
    assert persist.restore() == []
    persist.save()  # no-op, no network

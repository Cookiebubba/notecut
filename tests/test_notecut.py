"""End-to-end tests against a live server on a temp copy of the bundled demo.
Every test talks HTTP, the same way the page and the CLI do; nothing reaches into the ledger by hand.
"""
import gzip, json, shutil, socket, threading, urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from notecut import server as S

DEMO = Path(S.__file__).parent / "examples" / "demo"


def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


@pytest.fixture
def srv(tmp_path):
    """A server on its own copy of the demo. The module globals (ROOT etc.) are process-wide, so tests run serially."""
    root = tmp_path / "proj"
    shutil.copytree(DEMO, root)
    S.set_root(root)
    port = _free_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), S.make_handler(S.ConfigLoader()))
    httpd.daemon_threads = True
    t = threading.Thread(target=httpd.serve_forever, daemon=True); t.start()
    try:
        yield {"root": root, "url": f"http://127.0.0.1:{port}"}
    finally:
        httpd.shutdown(); httpd.server_close()


def req(base, path, body=None, headers=None, method=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(base + path, data=data, method=method or ("POST" if data else "GET"),
                               headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(r) as resp:
            raw = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            return resp.status, dict(resp.headers), raw
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def jget(base, path):
    code, _, raw = req(base, path); assert code == 200, (path, code, raw[:200]); return json.loads(raw)


# ------------------------------------------------------------------ pages + payload
def test_health_and_pages(srv):
    h = jget(srv["url"], "/api/health")
    assert h["ok"] is True and h["videos"] == 1
    for p in ("/", "/v/demo"):
        code, hdr, raw = req(srv["url"], p)
        assert code == 200 and "text/html" in hdr["Content-Type"] and b"Note Cut" in raw
    assert req(srv["url"], "/v/nope")[0] == 404
    assert req(srv["url"], "/api/../etc")[0] == 404


def test_payload_words_edl_and_splices(srv):
    d = jget(srv["url"], "/api/demo")
    assert d["words"] and all({"w", "s", "e"} <= set(w) for w in d["words"])
    kept = [w for w in d["words"] if w.get("kept")]
    assert 0 < len(kept) < len(d["words"]), "the demo EDL removes something, so some words must be cut"
    assert 25 < d["clip_dur"] < 35 and len(d["splices"]) == 3, "4 keep segments (stammer + 2 dead-air removals) -> 3 splices"
    assert d["peaks_url"] == "/peaks/demo.json", "peaks are lazy: the page fetches them after first paint"
    pk = jget(srv["url"], d["peaks_url"]); assert pk["hz"] == 50 and len(pk["peak"]) == pk["n"]


def test_payload_is_gzipped_when_asked(srv):
    code, hdr, raw = req(srv["url"], "/api/demo", headers={"Accept-Encoding": "gzip"})
    assert code == 200 and json.loads(raw)["id"] == "demo"
    r = urllib.request.Request(srv["url"] + "/api/demo", headers={"Accept-Encoding": "gzip"})
    with urllib.request.urlopen(r) as resp:
        assert resp.headers.get("Content-Encoding") == "gzip"


# ------------------------------------------------------------------ media: Range is load-bearing
def test_media_range_206_and_416(srv):
    code, hdr, raw = req(srv["url"], "/media/demo", headers={"Range": "bytes=0-99"})
    assert code == 206 and len(raw) == 100 and hdr["Content-Range"].startswith("bytes 0-99/")
    size = int(hdr["Content-Range"].rsplit("/", 1)[1])
    code, hdr, _ = req(srv["url"], "/media/demo", headers={"Range": f"bytes={size + 10}-"})
    assert code == 416 and hdr["Content-Range"] == f"bytes */{size}"
    code, hdr, raw = req(srv["url"], "/media/demo")
    assert code == 200 and hdr["Accept-Ranges"] == "bytes" and len(raw) == size
    assert raw[4:8] == b"ftyp", "served bytes are the mp4, not an error page"


def test_pins_sprite_peaks_logo_served(srv):
    for p, magic in (("/pins/demo/c0004.jpg", b"\xff\xd8"), ("/thumbs/demo.jpg", b"\xff\xd8"), ("/logo.png", b"\x89PNG")):
        code, _, raw = req(srv["url"], p)
        assert code == 200 and raw.startswith(magic), p
    assert jget(srv["url"], "/peaks/demo.json")["hz"] == 50


# ------------------------------------------------------------------ ledger fold: saved / sent / done
def test_fold_statuses_from_demo_ledger(srv):
    rows = {r["id"]: r for r in jget(srv["url"], "/api/demo/comments")["comments"]}
    assert rows["c0001"]["status"] == "done" and rows["c0001"]["done_note"].startswith("extended")
    assert rows["c0002"]["status"] == "sent"
    assert rows["c0003"]["status"] == "saved"
    assert rows["c0004"]["status"] == "sent" and rows["c0004"]["kind"] == "pin" and rows["c0004"]["shot"]


def test_comment_lifecycle_save_send_complete_delete(srv):
    u = srv["url"]
    code, _, raw = req(u, "/api/demo/comment", {"kind": "point", "clip_t": 5.0, "body": "draft", "notify": False})
    cid = json.loads(raw)["comment"]["id"]; assert code == 200 and cid == "c0005"
    feed_before = jget(u, "/api/feed")["total"]
    assert [r for r in jget(u, "/api/demo/comments")["comments"] if r["id"] == cid][0]["status"] == "saved"
    assert jget(u, "/api/feed")["total"] == feed_before, "saving must not reach the agent feed"

    req(u, "/api/demo/send", {"ref": cid})
    assert [r for r in jget(u, "/api/demo/comments")["comments"] if r["id"] == cid][0]["status"] == "sent"
    feed = jget(u, f"/api/feed?since={feed_before}")
    assert feed["total"] == feed_before + 1 and feed["items"][0]["id"] == cid and feed["items"][0]["video"] == "demo"

    req(u, "/api/demo/complete", {"ref": cid, "note": "did it", "by": "t-agent"})
    row = [r for r in jget(u, "/api/demo/comments")["comments"] if r["id"] == cid][0]
    assert row["status"] == "done" and row["done_note"] == "did it"

    req(u, "/api/demo/delete", {"ref": cid})
    assert cid not in {r["id"] for r in jget(u, "/api/demo/comments")["comments"]}

    ledger = (srv["root"] / "data" / "demo" / "comments.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(ledger) == 5 + 4, "append-only: point + send + complete + delete, nothing rewritten"


def test_empty_comment_rejected(srv):
    code, _, raw = req(srv["url"], "/api/demo/comment", {"kind": "point", "clip_t": 1.0, "body": "   "})
    assert code == 400 and b"empty" in raw


def test_transcript_comment_keeps_source_times_and_remaps_cur_t(srv):
    u = srv["url"]
    words = jget(u, "/api/demo")["words"]
    kept = [w for w in words if w.get("kept")][10:13]
    body = {"kind": "transcript", "src_start": kept[0]["s"], "src_end": kept[-1]["e"], "i0": kept[0]["i"], "i1": kept[-1]["i"],
            "sel": " ".join(w["w"] for w in kept), "body": "trim this", "notify": True}
    cid = json.loads(req(u, "/api/demo/comment", body)[2])["comment"]["id"]
    row = [r for r in jget(u, "/api/demo/comments")["comments"] if r["id"] == cid][0]
    assert row["status"] == "sent" and row["src_start"] == kept[0]["s"]
    assert row["cur_t"] is not None and abs(row["cur_t"] - kept[0]["cut_s"]) < 0.05, "cur_t lands where the word plays on the live cut"


# ------------------------------------------------------------------ state: set / merge / log
def test_state_replace_merge_and_log(srv):
    u = srv["url"]
    before = jget(u, "/api/demo/state")["state"]
    assert before["renders"]["current"]["version"] == "v2"
    code, _, raw = req(u, "/api/demo/state", {"merge": {"renders": {"current": {"version": "v3"}}}, "by": "t-agent", "note": "bump"})
    st = json.loads(raw)["state"]
    assert code == 200 and st["renders"]["current"]["version"] == "v3"
    assert st["renders"]["current"]["file"] == before["renders"]["current"]["file"], "merge keeps sibling keys"
    assert st["renders"]["lineage"] == before["renders"]["lineage"] and st["updated_by"] == "t-agent"
    code, _, raw = req(u, "/api/demo/state", {"state": {"only": 1}, "by": "t-agent"})
    assert json.loads(raw)["state"].keys() >= {"only", "updated_at", "updated_by"} and "renders" not in json.loads(raw)["state"]
    assert req(u, "/api/demo/state", {"nonsense": 1})[0] == 400
    log = [json.loads(x) for x in (srv["root"] / "data" / "demo" / "state_log.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [x["op"] for x in log[-2:]] == ["merge", "replace"] and log[-2]["note"] == "bump" and log[-2]["keys"] == ["renders"]


def test_handoff_reflects_ledger_and_state(srv):
    code, hdr, raw = req(srv["url"], "/api/demo/handoff")
    md = raw.decode("utf-8")
    assert code == 200 and "text/markdown" in hdr["Content-Type"]
    assert md.startswith("# Note Cut handoff: demo")
    assert "## Comments: 4 live, 1 done, 1 saved-not-sent" in md
    assert "| c0003 | saved |" in md and "| c0001 | done |" in md
    assert "## Rules that bind this edit" in md and "## Renders" in md and '"version": "v2"' in md
    # a state write shows up on the next read, no restart
    req(srv["url"], "/api/demo/state", {"merge": {"summary": "CHANGED-SUMMARY"}})
    assert "CHANGED-SUMMARY" in req(srv["url"], "/api/demo/handoff")[2].decode("utf-8")


# ------------------------------------------------------------------ config: hot reload + tolerance
def test_config_hot_reload_and_missing_optional_assets(srv, tmp_path):
    u, root = srv["url"], srv["root"]
    assert [v["id"] for v in jget(u, "/api/videos")["videos"]] == ["demo"]
    cfg = json.loads((root / "notecut.json").read_text(encoding="utf-8"))
    cfg["videos"]["bare"] = {"title": "no transcript, no edl", "media": "assets/demo/source.mp4"}
    import os, time
    (root / "notecut.json").write_text(json.dumps(cfg), encoding="utf-8")
    os.utime(root / "notecut.json", (time.time() + 2, time.time() + 2))   # mtime granularity on some filesystems
    ids = {v["id"] for v in jget(u, "/api/videos")["videos"]}
    assert ids == {"demo", "bare"}, "new entry visible without a restart"
    d = jget(u, "/api/bare")
    assert d["words"] == [] and d["clip_dur"] > 40 and d["splices"] == [], "no EDL -> the whole 42 s source plays"
    assert req(u, "/v/bare")[0] == 200 and req(u, "/media/bare", headers={"Range": "bytes=0-1"})[0] == 206
    assert jget(u, "/api/bare/comments")["comments"] == []
    assert req(u, "/api/bare/handoff")[0] == 200


def test_relative_paths_resolve_against_root(srv):
    cfg = S.load_config(srv["root"])
    assert Path(cfg["demo"]["media"]).is_absolute() and Path(cfg["demo"]["media"]).exists()
    assert Path(cfg["demo"]["media"]).parent.parent.parent == srv["root"]


# ------------------------------------------------------------------ CLI (no server needed)
def test_cli_init_creates_project(tmp_path):
    from notecut.cli import main
    d = tmp_path / "p"
    assert main(["init", str(d)]) == 0
    assert (d / "notecut.json").exists() and (d / "data").is_dir()
    assert json.loads((d / "notecut.json").read_text(encoding="utf-8")) == {"videos": {}}

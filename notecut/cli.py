"""notecut - one command for humans and agents.

Project (runs on the box that holds the media):
  notecut init [DIR]                                    create notecut.json, data/, assets/
  notecut serve [--root DIR] [--host H] [--port N]      run the viewer + API (default 0.0.0.0:8808)
  notecut add ID --media FILE [--title T] [--transcript words.json] [--edl edl.json]
              [--prior-edl old.json] [--section-start S] [--group G] [--no-prep] [--copy-media]
  notecut prep ID [--height 720]                        (re)build proxy / sprite / peaks for an id
  notecut transcribe ID [--model large-v3-turbo] [--device auto]   word timestamps via faster-whisper (optional extra)
  notecut demo [--dir ./notecut-demo] [--port N]        unpack the bundled sample project and serve it

Client (works from anywhere that can reach the server; env NOTECUT_URL, default http://127.0.0.1:8808):
  notecut videos                                        ids the server knows + when their state last changed
  notecut handoff ID                                    Markdown brief: resume an edit from this alone
  notecut comments ID [--open]                          folded comment rows (--open = sent and not done)
  notecut state get ID
  notecut state set ID FILE.json [--by NAME] [--note TEXT]      replace the whole state document
  notecut state merge ID FILE.json|'{json}' [--by ..] [--note ..]  deep-merge (lists are replaced)
  notecut complete ID COMMENT_ID NOTE...                mark a comment done (green for the reviewer)
  notecut log ID TEXT...                                append a dated line to state.log
  notecut watch [--since N]                             tail the feed of sent comments (one line each)
  notecut url ID                                        the link to send the reviewer

Exit codes: 0 ok, 1 usage, 2 network/http, 3 missing tool (ffmpeg / faster-whisper).
"""
from __future__ import annotations
import argparse, json, os, shutil, sys, time, urllib.error, urllib.request
from datetime import date
from pathlib import Path

from . import __version__
from . import server as S

BASE = os.environ.get("NOTECUT_URL", "http://127.0.0.1:8808").rstrip("/")
EXAMPLES = Path(__file__).resolve().parent / "examples"


# ---------------------------------------------------------------- HTTP client
def call(path, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method="POST" if data else "GET",
                                 headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode("utf-8", "replace")
            return raw if "json" not in (r.headers.get("Content-Type") or "") else json.loads(raw)
    except urllib.error.HTTPError as e:
        sys.stderr.write(f"HTTP {e.code} {path}: {e.read().decode('utf-8', 'replace')[:300]}\n"); sys.exit(2)
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        sys.stderr.write(f"network: {e} ({BASE}) - is `notecut serve` running? set NOTECUT_URL for a remote server\n"); sys.exit(2)


def _tc(t):
    return "-" if t is None else f"{int(t // 60)}:{t % 60:05.2f}"


def load_json_arg(s):
    if s.strip().startswith("{"):
        return json.loads(s)
    with open(s, encoding="utf-8-sig") as fh:
        return json.load(fh)


# ---------------------------------------------------------------- project commands
def _root(a):
    r = Path(getattr(a, "root", None) or os.environ.get("NOTECUT_ROOT") or ".").resolve()
    return r


def cmd_init(a):
    root = Path(a.dir).resolve(); root.mkdir(parents=True, exist_ok=True)
    (root / "data").mkdir(exist_ok=True); (root / "assets").mkdir(exist_ok=True)
    if not S.config_path(root).exists():
        S.set_root(root); S.save_config({"videos": {}})
        print(f"created {S.config_path(root)}")
    else:
        print(f"exists  {S.config_path(root)}")
    print(f"next: notecut add <id> --media <file.mp4> --root {root}   then   notecut serve --root {root}")
    return 0


def cmd_serve(a):
    S.serve(a.host, a.port, _root(a))
    return 0


def _read_doc(root):
    p = S.config_path(root)
    doc = json.loads(p.read_text(encoding="utf-8-sig")) if p.exists() else {"videos": {}}
    if "videos" not in doc:
        doc = {"videos": doc}
    return doc


def _rel(root, p):
    p = Path(p).resolve()
    try:
        return str(p.relative_to(root)).replace(os.sep, "/")
    except ValueError:
        return str(p)


def cmd_add(a):
    root = _root(a); S.set_root(root)
    if not root.exists():
        sys.stderr.write(f"no project at {root}; run `notecut init {root}` first\n"); return 1
    media = Path(a.media).resolve()
    if not media.exists():
        sys.stderr.write(f"media not found: {media}\n"); return 1
    if not a.no_prep and not S.media_duration(media) and not shutil.which("ffprobe"):
        sys.stderr.write("ffmpeg/ffprobe not on PATH: install it, or pass --no-prep to serve the file as-is\n"); return 3
    adir = root / "assets" / a.id; adir.mkdir(parents=True, exist_ok=True)
    entry = {"title": a.title or media.stem, "source": str(media)}
    if a.copy_media:
        shutil.copy2(media, adir / media.name); entry["source"] = _rel(root, adir / media.name)
    for key, val in (("transcript", a.transcript), ("edl", a.edl), ("prior_edl", a.prior_edl)):
        if val:
            src = Path(val).resolve()
            if not src.exists():
                sys.stderr.write(f"{key} not found: {src}\n"); return 1
            dst = adir / {"transcript": "words.json", "edl": "edl.json", "prior_edl": "prior_edl.json"}[key]
            if src != dst.resolve():
                shutil.copy2(src, dst)
            entry[key] = _rel(root, dst)
    if a.section_start is not None:
        entry["section_start"] = float(a.section_start)
    if a.group:
        entry["group"] = a.group
    if a.no_prep:
        entry["media"] = entry["source"]
        d = S.media_duration(media)
        if d: entry["duration"] = round(d, 3)
    else:
        from . import prep
        if not prep.have_ffmpeg():
            sys.stderr.write("ffmpeg/ffprobe not on PATH (needed for --prep); install it or pass --no-prep\n"); return 3
        print(f"prep {a.id}: proxy + sprite + peaks from {media.name} ...", flush=True)
        frag, info = prep.prepare(media, adir, height=a.height)
        for k in ("media", "sprite", "peaks"):
            if k in frag: frag[k] = _rel(root, frag[k])
        entry.update(frag)
        print(f"  {info['width']}x{info['height']} {info['duration']:.1f}s  audio={info['audio']}")
    doc = _read_doc(root)
    doc["videos"][a.id] = {**doc["videos"].get(a.id, {}), **entry}
    S.save_config(doc, root)
    print(f"added {a.id} -> {S.config_path(root)}")
    print(f"open  {BASE}/v/{a.id}   (server picks the change up without a restart)")
    if not entry.get("transcript"):
        print(f"tip   notecut transcribe {a.id}   gives the reviewer word-level select (needs `pip install notecut[asr]`)")
    return 0


def cmd_prep(a):
    root = _root(a); S.set_root(root)
    doc = _read_doc(root); ent = doc["videos"].get(a.id)
    if not ent:
        sys.stderr.write(f"unknown id {a.id}\n"); return 1
    from . import prep
    if not prep.have_ffmpeg():
        sys.stderr.write("ffmpeg/ffprobe not on PATH\n"); return 3
    src = ent.get("source") or ent.get("media")
    srcp = Path(src) if Path(src).is_absolute() else root / src
    frag, info = prep.prepare(srcp, root / "assets" / a.id, height=a.height)
    for k in ("media", "sprite", "peaks"):
        if k in frag: frag[k] = _rel(root, frag[k])
    ent.update(frag); S.save_config(doc, root)
    print(json.dumps({"id": a.id, **frag, "width": info["width"], "height": info["height"]}, indent=1)); return 0


def cmd_transcribe(a):
    root = _root(a); S.set_root(root)
    doc = _read_doc(root); ent = doc["videos"].get(a.id)
    if not ent:
        sys.stderr.write(f"unknown id {a.id}\n"); return 1
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        sys.stderr.write("faster-whisper not installed: pip install 'notecut[asr]' (GPU: also a CUDA-enabled torch/ctranslate2)\n"); return 3
    src = ent.get("source") or ent["media"]
    srcp = Path(src) if Path(src).is_absolute() else root / src
    device = a.device
    if device == "auto":
        try:
            import ctranslate2
            device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
        except Exception:
            device = "cpu"
    compute = "float16" if device == "cuda" else "int8"
    print(f"transcribe {a.id}: {a.model} on {device} ...", flush=True)
    model = WhisperModel(a.model, device=device, compute_type=compute)
    segs, info = model.transcribe(str(srcp), word_timestamps=True, language=a.language or None, vad_filter=False)
    words = []
    for sg in segs:
        for w in (sg.words or []):
            words.append({"w": w.word.strip(), "s": round(w.start, 3), "e": round(w.end, 3), "p": round(w.probability, 3)})
    out = root / "assets" / a.id / "words.json"; out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"source": str(srcp), "model": a.model, "language": info.language, "words": words},
                              ensure_ascii=False, indent=0), encoding="utf-8")
    ent["transcript"] = _rel(root, out); S.save_config(doc, root)
    print(f"{len(words)} words -> {out}"); return 0


def cmd_demo(a):
    dst = Path(a.dir).resolve()
    src = EXAMPLES / "demo"
    if not src.exists():
        sys.stderr.write(f"bundled demo missing at {src}\n"); return 1
    if not dst.exists():
        shutil.copytree(src, dst)
        print(f"unpacked demo project -> {dst}")
    else:
        print(f"using existing {dst}")
    print(f"\n  open  http://127.0.0.1:{a.port}/v/demo\n  agent NOTECUT_URL=http://127.0.0.1:{a.port} notecut handoff demo\n", flush=True)
    S.serve(a.host, a.port, dst); return 0


# ---------------------------------------------------------------- client commands
def cmd_videos(a):
    for v in call("/api/videos")["videos"]:
        print(f"{v['id']:16} state_updated={v['state_updated'] or '-':22} {v['title']}")
    return 0


def cmd_handoff(a):
    print(call(f"/api/{a.id}/handoff")); return 0


def cmd_comments(a):
    rows = call(f"/api/{a.id}/comments")["comments"]
    if a.open:
        rows = [r for r in rows if r["status"] == "sent" and not r.get("resolved")]
    if a.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1)); return 0
    for r in rows:
        print(f"{r['id']:7} {r['status']:5} {_tc(r.get('cur_t')):>9} {r['kind']:10} {(r.get('body') or '')[:110]!s}"
              + (f"  // {r['done_note'][:60]}" if r.get("done_note") else ""))
    print(f"-- {len(rows)} rows"); return 0


def cmd_state(a):
    if a.op == "get":
        print(json.dumps(call(f"/api/{a.id}/state")["state"], indent=1, ensure_ascii=False)); return 0
    payload = load_json_arg(a.doc)
    out = call(f"/api/{a.id}/state", {"by": a.by, "note": a.note, ("state" if a.op == "set" else "merge"): payload})
    print(f"ok {a.op} by={a.by} updated_at={out['state'].get('updated_at')}"); return 0


def cmd_log(a):   # merge replaces lists, so read-modify-write the log here
    cur = call(f"/api/{a.id}/state")["state"]; log = list(cur.get("log") or [])
    log.append(f"{date.today().isoformat()} {' '.join(a.text)}")
    out = call(f"/api/{a.id}/state", {"by": a.by, "note": "log append", "merge": {"log": log}})
    print(f"ok log ({len(log)} lines) updated_at={out['state'].get('updated_at')}"); return 0


def cmd_complete(a):
    print(json.dumps(call(f"/api/{a.id}/complete", {"ref": a.comment, "note": " ".join(a.note), "by": a.by}))); return 0


def cmd_watch(a):
    since = a.since
    if since is None:
        since = call("/api/feed?since=0")["total"]     # start at the end: only NEW sends are reported
    print(f"watching {BASE} feed from line {since} (ctrl-c stops)", flush=True)
    while True:
        r = call(f"/api/feed?since={since}")
        for it in r["items"]:
            print(f"NEW {it.get('at','')} {it.get('summary') or json.dumps(it.get('rec'))}", flush=True)
        since = r["total"]
        if a.once:
            return 0
        time.sleep(a.interval)


def cmd_url(a):
    print(f"{BASE}/v/{a.id}"); return 0


# ---------------------------------------------------------------- argparse
def build_parser():
    ap = argparse.ArgumentParser(prog="notecut", description="Note Cut - review viewer for humans, edit-state backend for agents")
    ap.add_argument("--version", action="version", version=f"notecut {__version__}")
    sp = ap.add_subparsers(dest="cmd", metavar="COMMAND")

    def root_arg(p):
        p.add_argument("--root", help="project directory (default: NOTECUT_ROOT or .)")

    p = sp.add_parser("init", help="create a project directory"); p.add_argument("dir", nargs="?", default="."); p.set_defaults(fn=cmd_init)
    p = sp.add_parser("serve", help="run the server"); root_arg(p)
    p.add_argument("--host", default="0.0.0.0"); p.add_argument("--port", type=int, default=8808); p.set_defaults(fn=cmd_serve)
    p = sp.add_parser("add", help="register a video (and build its proxy/sprite/peaks)"); root_arg(p)
    p.add_argument("id"); p.add_argument("--media", required=True); p.add_argument("--title")
    p.add_argument("--transcript"); p.add_argument("--edl"); p.add_argument("--prior-edl", dest="prior_edl")
    p.add_argument("--section-start", dest="section_start", type=float); p.add_argument("--group")
    p.add_argument("--no-prep", dest="no_prep", action="store_true", help="serve the file as-is (no ffmpeg)")
    p.add_argument("--copy-media", dest="copy_media", action="store_true", help="copy the source into assets/<id>/")
    p.add_argument("--height", type=int, default=720); p.set_defaults(fn=cmd_add)
    p = sp.add_parser("prep", help="rebuild proxy/sprite/peaks"); root_arg(p); p.add_argument("id")
    p.add_argument("--height", type=int, default=720); p.set_defaults(fn=cmd_prep)
    p = sp.add_parser("transcribe", help="word timestamps with faster-whisper"); root_arg(p); p.add_argument("id")
    p.add_argument("--model", default="large-v3-turbo"); p.add_argument("--device", default="auto"); p.add_argument("--language", default="en")
    p.set_defaults(fn=cmd_transcribe)
    p = sp.add_parser("demo", help="unpack + serve the bundled sample"); p.add_argument("--dir", default="./notecut-demo")
    p.add_argument("--host", default="0.0.0.0"); p.add_argument("--port", type=int, default=8808); p.set_defaults(fn=cmd_demo)

    p = sp.add_parser("videos", help="list ids on the server"); p.set_defaults(fn=cmd_videos)
    p = sp.add_parser("handoff", help="Markdown brief for one video"); p.add_argument("id"); p.set_defaults(fn=cmd_handoff)
    p = sp.add_parser("comments", help="folded comment rows"); p.add_argument("id"); p.add_argument("--open", action="store_true")
    p.add_argument("--json", action="store_true"); p.set_defaults(fn=cmd_comments)
    p = sp.add_parser("state", help="get / set / merge state.json"); p.add_argument("op", choices=["get", "set", "merge"]); p.add_argument("id")
    p.add_argument("doc", nargs="?"); p.add_argument("--by", default=os.environ.get("NOTECUT_BY", "agent")); p.add_argument("--note", default="")
    p.set_defaults(fn=cmd_state)
    p = sp.add_parser("complete", help="mark a comment done"); p.add_argument("id"); p.add_argument("comment"); p.add_argument("note", nargs="*")
    p.add_argument("--by", default=os.environ.get("NOTECUT_BY", "agent")); p.set_defaults(fn=cmd_complete)
    p = sp.add_parser("log", help="append a dated line to state.log"); p.add_argument("id"); p.add_argument("text", nargs="+")
    p.add_argument("--by", default=os.environ.get("NOTECUT_BY", "agent")); p.set_defaults(fn=cmd_log)
    p = sp.add_parser("watch", help="tail sent comments"); p.add_argument("--since", type=int); p.add_argument("--interval", type=float, default=2.0)
    p.add_argument("--once", action="store_true"); p.set_defaults(fn=cmd_watch)
    p = sp.add_parser("url", help="print the reviewer link"); p.add_argument("id"); p.set_defaults(fn=cmd_url)
    return ap


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):   # Windows consoles default to cp1252 and mangle em-dashes in comments
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = build_parser()
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help(); return 1
    if a.cmd == "state" and a.op != "get" and not a.doc:
        ap.error("state set/merge needs a JSON file or inline '{...}'")
    try:
        return a.fn(a) or 0
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())

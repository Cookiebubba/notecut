"""Note Cut server: the review viewer humans use and the edit-state backend agents resume from.

  * video player + timeline strip; a comment at the exact playhead becomes a marker on the strip
  * transcript under the video, words that are IN the cut highlighted; drag across it to make a
    structured EDIT comment ("extend the cut to <word>") an agent can act on without guessing
  * pins on the frame (with a screenshot), an explicit Send-to-agent step, Save = private draft
  * comments persist append-only (data/<id>/comments.jsonl) and Sent ones also go to data/feed.jsonl
  * per-video state.json + state_log.jsonl + a generated Markdown handoff (/api/<id>/handoff)

stdlib only. Run `notecut serve --root <project>`; the project root holds notecut.json, data/, assets/.
"""
from __future__ import annotations
import argparse, gzip, json, mimetypes, os, re, subprocess, threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

STATIC = Path(__file__).resolve().parent / "static"
CHUNK = 512 * 1024
_LOCK = threading.Lock()

# The project root is resolved once per process (serve() calls set_root); every path below hangs off it.
ROOT = Path(os.environ.get("NOTECUT_ROOT", ".")).resolve()
REVIEW_DIR = ROOT / "data"
FEED = REVIEW_DIR / "feed.jsonl"
LOGO = STATIC / "logo.png"


def set_root(root):
    """Point the module at a project root: <root>/notecut.json, <root>/data, <root>/assets."""
    global ROOT, REVIEW_DIR, FEED, LOGO
    ROOT = Path(root).resolve()
    REVIEW_DIR = Path(os.environ.get("NOTECUT_DATA") or (ROOT / "data"))
    FEED = REVIEW_DIR / "feed.jsonl"
    custom = ROOT / "logo.png"
    LOGO = custom if custom.exists() else STATIC / "logo.png"
    return ROOT


def config_path(root=None):
    return Path(root or ROOT) / "notecut.json"


def _static(name):
    return (STATIC / name).read_text(encoding="utf-8")


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


PATH_KEYS = ("media", "transcript", "edl", "prior_edl", "sprite", "peaks", "poster")


def load_config(root=None):
    """notecut.json -> {id: cfg}. Accepts {"videos": {...}} or a flat {id: {...}} map. Relative paths resolve
    against the project root, so a project directory can be moved, zipped or rsynced as a unit."""
    root = Path(root or ROOT)
    p = config_path(root)
    if not p.exists():
        return {}
    doc = json.loads(p.read_text(encoding="utf-8-sig"))
    raw = doc.get("videos", doc) if isinstance(doc, dict) else {}
    cfg = {}
    for k, v in raw.items():
        if not isinstance(v, dict) or not re.fullmatch(r"[\w-]+", k):
            continue
        v = dict(v)
        for key in PATH_KEYS:
            if v.get(key):
                q = Path(v[key])
                v[key] = q if q.is_absolute() else (root / q)
        cfg[k] = v
    return cfg


def save_config(cfg_doc, root=None):
    """Write notecut.json atomically. cfg_doc is the on-disk shape ({"videos": {...}}), paths as given."""
    p = config_path(root)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg_doc, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def media_duration(path):
    """Seconds, via ffprobe; None when ffprobe is missing or the file is not probeable."""
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                             capture_output=True, text=True, timeout=60)
        return float(out.stdout.strip()) if out.returncode == 0 and out.stdout.strip() else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def _load_transcript(cfg):
    """{"words":[{w,s,e}], ...}; an absent transcript is an empty word list, not an error."""
    t = cfg.get("transcript")
    if t and Path(t).exists():
        return json.loads(Path(t).read_text(encoding="utf-8-sig"))
    return {"words": []}


def _load_edl(cfg):
    """{"keep":[{start,end}]}; without an EDL the whole media is the cut (needs cfg.duration or ffprobe)."""
    e = cfg.get("edl")
    if e and Path(e).exists():
        return json.loads(Path(e).read_text(encoding="utf-8-sig"))
    dur = cfg.get("duration") or media_duration(cfg["media"]) or 0.0
    return {"keep": [{"start": 0.0, "end": float(dur)}]}


def build_payload(vid, cfg):
    """words with kept-flag + cut-time, contiguous kept spans, and the current comments."""
    tdoc = _load_transcript(cfg)
    words = tdoc["words"]
    edl = _load_edl(cfg)
    keep = edl["keep"]
    # cut-time offset for each kept source-range
    segs, off = [], 0.0
    for k in keep:
        segs.append((k["start"], k["end"], off)); off += k["end"] - k["start"]
    clip_dur = off

    def cut_time(t):
        for s, e, o in segs:
            if s - 1e-6 <= t <= e + 1e-6:
                return round(o + (t - s), 3)
        return None

    def seg_of(t):
        for s, e, o in segs:
            if s - 1e-6 <= t <= e + 1e-6:
                return s, e, o
        return None

    out_words = []
    for i, w in enumerate(words):
        mid = (w["s"] + w["e"]) / 2
        sg = seg_of(mid)
        ct = round(sg[2] + (mid - sg[0]), 3) if sg else None
        row = {"i": i, "w": w["w"], "s": round(w["s"], 3), "e": round(w["e"], 3), "kept": ct is not None, "cut_t": ct}
        if sg:  # measured word extent on the cut (clipped to its keep segment) for the waveform strip
            s0, e0, o = sg
            row["cut_s"] = round(o + (max(w["s"], s0) - s0), 3); row["cut_e"] = round(o + (min(w["e"], e0) - s0), 3)
            if w.get("snapped") is not None:
                row["snapped"] = bool(w["snapped"])
        out_words.append(row)
    # splice points on the cut: where one keep segment ends and the next begins
    splices = [{"t": round(o, 3), "src_out": round(segs[j - 1][1], 3), "src_in": round(s, 3)}
               for j, (s, e, o) in enumerate(segs) if j > 0]
    # window the transcript to the clip +/- margin_words (up to 100) so he can pull a better in/out
    margin = int(cfg.get("margin_words", 100))
    kept_ix = [j for j, w in enumerate(out_words) if w["kept"]]
    ordered = bool(edl.get("run", {}).get("params", {}).get("ordered")) or bool(cfg.get("ordered"))
    if ordered and kept_ix:
        # sequence cut (VOD-clipping lane): keep[] is in EDIT order across a whole stream, so the transcript follows
        # the cut - each keep segment's words +/- margin, in keep order - instead of one window over 6 hours of words.
        by_seg = {}
        for j in kept_ix:
            s0 = seg_of((out_words[j]["s"] + out_words[j]["e"]) / 2)[0]
            by_seg.setdefault(s0, [j, j]); by_seg[s0][1] = j
        window, seen = [], set()
        for s0, _e, _o in segs:
            if s0 not in by_seg:
                continue
            lo, hi = by_seg[s0]
            for j in range(max(0, lo - margin), min(len(out_words) - 1, hi + margin) + 1):
                if j not in seen:
                    seen.add(j); window.append(out_words[j])
    elif kept_ix:
        lo = max(0, kept_ix[0] - margin); hi = min(len(out_words) - 1, kept_ix[-1] + margin)
        window = out_words[lo:hi + 1]
    else:
        window = out_words
    comments = read_comments(vid)
    # --- anchor every comment to its CURRENT position on the live cut ---
    # point/pin comments carry a clip_t from the version they were made on (prior_edl);
    # map that clip_t -> source time (via prior keep) -> current clip time (via current keep).
    # transcript comments carry src_start -> map straight to current clip time.
    cur_segs = segs  # [(s,e,off)] of the current EDL
    prior = cfg.get("prior_edl")
    prior_segs = None
    if prior and Path(prior).exists():
        pk = json.loads(Path(prior).read_text(encoding="utf-8"))["keep"]
        prior_segs, po = [], 0.0
        for k in pk:
            prior_segs.append((k["start"], k["end"], po)); po += k["end"] - k["start"]

    def clip2src(ct, S):
        for s, e, o in S:
            if o - 1e-6 <= ct <= o + (e - s) + 1e-6:
                return s + (ct - o)
        return None

    def src2clip(t, S):
        best, bd = None, 1e9
        for s, e, o in S:
            if s - 1e-6 <= t <= e + 1e-6:
                return round(o + (t - s), 3)
            d = min(abs(t - s), abs(t - e))
            if d < bd:
                bd = d; best = o if t < s else o + (e - s)
        return round(best, 3) if best is not None else None

    for c in comments:
        k = c.get("kind")
        if k == "transcript" and c.get("src_start") is not None:
            c["cur_t"] = src2clip(c["src_start"], cur_segs)
        elif k in ("point", "pin") and c.get("clip_t") is not None:
            src = clip2src(c["clip_t"], prior_segs) if prior_segs else None
            c["cur_t"] = src2clip(src, cur_segs) if src is not None else c["clip_t"]

    # inserts (spliced full-screen segments, e.g. color-bar interrupts) stretch the live cut:
    # anything at/after an insert's position shifts later by its duration. Keep cur_t + clip_dur in sync.
    inserts = sorted(cfg.get("inserts", []), key=lambda x: x["at"])
    if inserts:
        def shifted(ct):
            return round(ct + sum(i["dur"] for i in inserts if ct >= i["at"] - 1e-6), 3)
        for c in comments:
            if c.get("cur_t") is not None:
                c["cur_t"] = shifted(c["cur_t"])
        for w in out_words:                      # words follow the splices too
            if w["cut_t"] is not None:
                w["cut_t"] = shifted(w["cut_t"])
                if "cut_s" in w:
                    w["cut_s"] = shifted(w["cut_s"]); w["cut_e"] = shifted(w["cut_e"])
        for sp in splices:
            sp["t"] = shifted(sp["t"])
        clip_dur += sum(i["dur"] for i in inserts)

    out = {
        "id": vid, "title": cfg["title"], "media_url": f"/media/{vid}",
        "media_name": Path(cfg["media"]).name, "section_start": cfg.get("section_start", 0.0),
        "clip_dur": round(clip_dur, 3), "words": window, "margin_words": margin,
        "keep": [{"start": k["start"], "end": k["end"]} for k in keep],
        "splices": splices, "inserts": inserts, "comments": comments,
        "words_snapped": bool(tdoc.get("snap")),
        "handoff_url": f"/api/{vid}/handoff", "state_url": f"/api/{vid}/state",
    }
    if cfg.get("peaks") and Path(cfg["peaks"]).exists():
        out["peaks_url"] = f"/peaks/{vid}.json"
    if cfg.get("sprite") and Path(cfg["sprite"]).exists():
        out["sprite"] = {"url": f"/thumbs/{vid}.jpg",
                         "interval": cfg.get("sprite_interval", 4),
                         "cols": cfg.get("sprite_cols", 20),
                         "tw": cfg.get("sprite_tw", 160), "th": cfg.get("sprite_th", 90)}
    return out


def comments_path(vid):
    d = REVIEW_DIR / vid; d.mkdir(parents=True, exist_ok=True)
    return d / "comments.jsonl"


def read_comments(vid):
    p = comments_path(vid)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def make_summary(vid, cfg, rec):
    ss = cfg.get("section_start", 0.0)
    body = rec.get("body", "")
    k = rec.get("kind")
    if k == "transcript":
        return f'[{vid}] EDIT {_tc(ss + rec["src_start"])}-{_tc(ss + rec["src_end"])} "{rec.get("sel","")[:60]}" :: {body}'
    if k == "pin":
        shot = f' shot={rec["shot"]}' if rec.get("shot") else ""
        return f'[{vid}] PIN @{_tc(rec.get("clip_t",0))} xy=({rec.get("x")},{rec.get("y")}){shot} :: {body}'
    return f'[{vid}] @{_tc(rec.get("clip_t",0))} (clip) :: {body}'


def _save_shot(vid, cid, dataurl):
    """Save a data:image/jpeg;base64,... screenshot to review/<vid>/pins/<cid>.jpg, return rel name."""
    import base64
    if not dataurl or "," not in dataurl:
        return None
    b64 = dataurl.split(",", 1)[1]
    d = REVIEW_DIR / vid / "pins"; d.mkdir(parents=True, exist_ok=True)
    (d / f"{cid}.jpg").write_bytes(base64.b64decode(b64))
    return f"pins/{cid}.jpg"


def _write_feed(vid, cfg, rec):
    FEED.parent.mkdir(parents=True, exist_ok=True)
    with open(FEED, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps({"at": _now(), "id": rec["id"], "video": vid,
                             "summary": make_summary(vid, cfg, rec), "rec": rec}, ensure_ascii=False) + "\n")


def add_comment(vid, cfg, rec):
    body = (rec.get("body") or "").strip()
    if not body:
        raise ValueError("empty comment")
    notify = bool(rec.get("notify", False))   # Save by default; Send-to-agent sets notify
    n = sum(1 for c in read_comments(vid) if c.get("kind") in ("point", "transcript", "pin"))
    rec = dict(rec); rec.pop("notify", None)
    cid = f"c{n+1:04d}"
    shot = rec.pop("shot", None)
    rec.update({"id": cid, "video": vid, "sent": notify, "at": _now()})
    if rec.get("kind") == "pin" and shot:
        rec["shot"] = _save_shot(vid, cid, shot)
    with _LOCK:
        with open(comments_path(vid), "a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        if notify:                              # only Send-to-agent hits the watchdog feed
            _write_feed(vid, cfg, rec)
    return rec


def unsent_comments(vid):
    all_ = read_comments(vid)
    deleted = {c["ref"] for c in all_ if c.get("kind") == "delete"}
    sent = {c["ref"] for c in all_ if c.get("kind") == "send"}
    return [c for c in all_ if c.get("kind") in ("point", "transcript", "pin")
            and c["id"] not in deleted and not (c.get("sent") or c["id"] in sent)]


def send_all(vid, cfg):
    us = unsent_comments(vid)
    with _LOCK:
        with open(comments_path(vid), "a", encoding="utf-8", newline="\n") as fh:
            for c in us:
                fh.write(json.dumps({"kind": "send", "ref": c["id"], "at": _now()}, ensure_ascii=False) + "\n")
        for c in us:
            _write_feed(vid, cfg, c)
    return len(us)


def send_comment(vid, cfg, ref):
    """Promote a previously-saved comment to the agent (append a send op + feed line)."""
    target = None
    for c in read_comments(vid):
        if c.get("id") == ref and c.get("kind") in ("point", "transcript", "pin"):
            target = c
    if not target:
        raise ValueError(f"no comment {ref}")
    with _LOCK:
        with open(comments_path(vid), "a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps({"kind": "send", "ref": ref, "at": _now()}, ensure_ascii=False) + "\n")
        _write_feed(vid, cfg, target)
    return target


def fold_comments(vid, cfg=None):
    """The comment ledger folded into one row per live comment (what an agent needs, no ledger parsing).
    status: saved | sent | done ; plus resolved flag, cur_t (position on the live cut) and the done note."""
    all_ = read_comments(vid)
    deleted = {c["ref"] for c in all_ if c.get("kind") == "delete"}
    sent = {c["ref"] for c in all_ if c.get("kind") == "send"}
    res, done = {}, {}
    for c in all_:
        if c.get("kind") == "resolve":
            res[c["ref"]] = bool(c.get("resolved", True))
        elif c.get("kind") == "complete":
            done[c["ref"]] = {"done": bool(c.get("done", True)), "note": c.get("note", ""), "at": c.get("at")}
    cur = {}
    if cfg is not None:
        try:
            cur = {c["id"]: c.get("cur_t") for c in build_payload(vid, cfg)["comments"] if c.get("id")}
        except Exception as e:                      # surface it, never silently lose positions
            print(f"WARN fold_comments({vid}): cur_t unavailable: {e!r}", flush=True)
    rows = []
    for c in all_:
        if c.get("kind") not in ("point", "transcript", "pin") or c["id"] in deleted:
            continue
        d = done.get(c["id"], {})
        st = "done" if d.get("done") else ("sent" if (c.get("sent") or c["id"] in sent) else "saved")
        row = {"id": c["id"], "kind": c["kind"], "status": st, "resolved": bool(res.get(c["id"])),
               "body": c.get("body", ""), "at": c.get("at"), "cur_t": cur.get(c["id"]),
               "done_note": d.get("note", ""), "done_at": d.get("at")}
        for k in ("clip_t", "src_start", "src_end", "sel", "x", "y", "shot", "edge", "old_i", "new_i"):
            if k in c:
                row[k] = c[k]
        rows.append(row)
    return rows


# ---- edit state: the machine-readable record of the edit, per video, owned by the agent ----
# state.json = current snapshot; state_log.jsonl = append-only history of every write (who/when/what keys).
# A fresh session (or another agent) resumes from GET /api/<id>/handoff with no chat context.
def state_path(vid):
    d = REVIEW_DIR / vid; d.mkdir(parents=True, exist_ok=True)
    return d / "state.json"


def read_state(vid):
    p = state_path(vid)
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8-sig"))


def _deep_merge(dst, src):
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_merge(dst[k], v)
        else:
            dst[k] = v
    return dst


def write_state(vid, body):
    """body: {"state": {...}} replaces; {"merge": {...}} deep-merges; optional "by" and "note"."""
    by = body.get("by", "agent"); note = body.get("note", "")
    with _LOCK:
        cur = read_state(vid)
        if "state" in body and isinstance(body["state"], dict):
            new, op = dict(body["state"]), "replace"
        elif "merge" in body and isinstance(body["merge"], dict):
            new, op = _deep_merge(cur, body["merge"]), "merge"
        else:
            raise ValueError("body needs 'state' (replace) or 'merge' (deep merge)")
        new["updated_at"] = _now(); new["updated_by"] = by
        p = state_path(vid)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(new, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, p)
        keys = sorted((body.get("state") or body.get("merge") or {}).keys())
        with open(p.parent / "state_log.jsonl", "a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps({"at": new["updated_at"], "by": by, "op": op, "keys": keys, "note": note},
                                ensure_ascii=False) + "\n")
    return new


def make_handoff(vid, cfg):
    """Markdown brief a fresh agent reads to resume this edit. Generated, never hand-maintained."""
    st = read_state(vid)
    rows = fold_comments(vid, cfg)
    ss = cfg.get("section_start", 0.0)
    L = [f"# Note Cut handoff: {vid}", "",
         f"Title: {cfg.get('title','')}", f"Generated: {_now()}  (state updated {st.get('updated_at','never')} by {st.get('updated_by','-')})",
         f"Preview: /v/{vid}   media: /media/{vid}   state: /api/{vid}/state   comments: /api/{vid}/comments", ""]
    if st.get("summary"):
        L += ["## Summary", st["summary"], ""]
    if st.get("resume"):
        L += ["## How to resume", st["resume"] if isinstance(st["resume"], str) else "\n".join(f"- {x}" for x in st["resume"]), ""]
    if st.get("open_items"):
        L += ["## Open items (owner / agent)"] + [f"- {x}" for x in st["open_items"]] + [""]
    if st.get("rules"):
        L += ["## Rules that bind this edit"] + [f"- {x}" for x in st["rules"]] + [""]
    # counts
    n = len(rows); nd = sum(1 for r in rows if r["status"] == "done"); ns = sum(1 for r in rows if r["status"] == "saved")
    L += [f"## Comments: {n} live, {nd} done, {ns} saved-not-sent (owner drafts: surface, do not act)", ""]
    L += ["| id | status | at (live cut) | kind | comment | done note |", "|---|---|---|---|---|---|"]
    for r in rows:
        where = _tc(r["cur_t"]) if r.get("cur_t") is not None else "-"
        if r["kind"] == "transcript":
            where += f" (src {_tc(ss + r.get('src_start', 0))}-{_tc(ss + r.get('src_end', 0))})"
        body = (r["body"] or "").replace("|", "/").replace("\n", " ")
        st_ = r["status"] + (" +resolved" if r["resolved"] else "")
        L.append(f"| {r['id']} | {st_} | {where} | {r['kind']} | {body[:140]} | {(r.get('done_note') or '').replace('|','/')[:80]} |")
    L.append("")
    for sec, key in (("Source", "source"), ("Transcript", "transcript"), ("EDL / timeline", "timeline"),
                     ("Inserts (splices that stretch the live cut)", "inserts"), ("Effects", "effects"),
                     ("Audio chain", "audio"), ("Renders", "renders"), ("Preview", "preview"),
                     ("Assets", "assets"), ("Tools / rebuild", "tools"), ("Log", "log")):
        if key in st:
            L += [f"## {sec}", "```json", json.dumps(st[key], ensure_ascii=False, indent=1), "```", ""]
    extra = {k: v for k, v in st.items() if k not in {"summary", "resume", "open_items", "rules", "source", "transcript",
             "timeline", "inserts", "effects", "audio", "renders", "preview", "assets", "tools", "log", "updated_at", "updated_by"}}
    if extra:
        L += ["## Other state", "```json", json.dumps(extra, ensure_ascii=False, indent=1), "```", ""]
    return "\n".join(L)


def _tc(s):
    s = max(0.0, float(s)); m = int(s // 60); return f"{m}:{s-m*60:05.2f}"


# ---- homepage: one card per video in the config, click -> /v/<id>. Same monochrome palette as the player. ----
HOME = _static("home.html")


def _jpeg_size(p):
    """(w, h) from the JPEG SOF marker, stdlib only; None if it isn't a baseline/progressive JPEG."""
    try:
        b = p.read_bytes()
        i = 2
        while i + 9 < len(b):
            if b[i] != 0xFF:
                return None
            mk = b[i + 1]; ln = int.from_bytes(b[i + 2:i + 4], "big")
            if mk in (0xC0, 0xC1, 0xC2):
                return int.from_bytes(b[i + 7:i + 9], "big"), int.from_bytes(b[i + 5:i + 7], "big")
            i += 2 + ln
    except Exception:
        pass
    return None


def _esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def home_html(cfg):
    """Cards grouped by cfg[...]['group'] (e.g. the client/channel). Poster = cfg 'poster' image, else a tile ~10% into
    the sprite. Counts come from the folded ledger; version/updated from state.json."""
    groups = {}
    for vid, c in cfg.items():
        dur = None
        try:
            dur = build_payload(vid, c)["clip_dur"]
        except Exception as e:
            print(f"WARN home({vid}): payload failed: {e!r}", flush=True)
        rows = fold_comments(vid, None)
        n_open = sum(1 for r in rows if r["status"] == "sent"); n_saved = sum(1 for r in rows if r["status"] == "saved")
        n_done = sum(1 for r in rows if r["status"] == "done")
        st = read_state(vid)
        cur = (st.get("renders") or {}).get("current", "")
        ver = ""
        if isinstance(cur, dict):            # schema: renders.current = {file, version?, ...}; a bare path also works
            ver = cur.get("version") or ""
            cur = cur.get("file") or cur.get("name") or ""
        ver = ver or (Path(str(cur)).stem if cur else "")
        if c.get("poster"):
            poster = f'style="background-image:url(/poster/{vid});background-size:cover"'
        elif c.get("sprite") and dur and Path(c["sprite"]).exists():
            iv, cols, tw, th = c.get("sprite_interval", 4), c.get("sprite_cols", 20), c.get("sprite_tw", 160), c.get("sprite_th", 90)
            idx = int((dur * 0.1) // iv); ci, ri = idx % cols, idx // cols
            iw, ih = _jpeg_size(Path(c["sprite"])) or (cols * tw, th)
            rows = max(1, ih // th)
            # CSS %-position aligns image point p with box point p: offset = (box - img) * p, so tile i of n -> i/(n-1)
            px = 100.0 * ci / (cols - 1) if cols > 1 else 0.0
            py = 100.0 * ri / (rows - 1) if rows > 1 else 0.0
            poster = (f'style="background-image:url(/thumbs/{vid}.jpg);background-size:{cols*100}% {rows*100}%;'
                      f'background-position:{px:.4f}% {py:.4f}%"')
        else:
            poster = 'style="background:#000"'
        pills = []
        if n_open: pills.append(f'<span class="pill open">{n_open} waiting on the agent</span>')
        if n_saved: pills.append(f'<span class=pill>{n_saved} saved</span>')
        if n_done: pills.append(f'<span class="pill done">{n_done} done</span>')
        if not rows: pills.append('<span class=pill>no comments yet</span>')
        upd = (st.get("updated_at") or "")[:16].replace("T", " ")
        card = (f'<a class=card href="/v/{vid}"><div class=poster {poster}>'
                + (f'<span class=dur>{int(dur//60)}:{int(dur%60):02d}</span>' if dur else "")
                + (f'<span class=ver>{_esc(ver)}</span>' if ver else "")
                + f'</div><div class=body><p class=t>{_esc(c.get("title",vid))}</p><div class=meta>{"".join(pills)}'
                + (f'<span>updated {_esc(upd)}</span>' if upd else "") + '</div></div></a>')
        groups.setdefault(c.get("group", "Videos"), []).append(card)
    if not groups:
        body = '<div class=empty>Nothing in the edit yet.</div>'
    else:
        body = "".join(f'<div class=sec>{_esc(g)}</div><div class=grid>{"".join(cs)}</div>' for g, cs in groups.items())
    return HOME.replace("__CARDS__", body)


def _serve_range(h, path):
    size = path.stat().st_size
    ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    rng = h.headers.get("Range"); start, end, partial = 0, size - 1, False
    if rng:
        m = re.match(r"bytes=(\d*)-(\d*)$", rng.strip())
        if m:
            g1, g2 = m.group(1), m.group(2)
            if g1:
                start = int(g1)
                if g2: end = min(int(g2), size - 1)
            elif g2:
                start = max(0, size - int(g2))
            if start >= size or start > end:
                h.send_response(416); h.send_header("Content-Range", f"bytes */{size}"); h.end_headers(); return
            partial = True
    h.send_response(206 if partial else 200)
    h.send_header("Content-Type", ctype); h.send_header("Accept-Ranges", "bytes")
    h.send_header("Content-Length", str(end - start + 1))
    if partial: h.send_header("Content-Range", f"bytes {start}-{end}/{size}")
    h.end_headers()
    if h.command == "HEAD": return
    left = end - start + 1
    with open(path, "rb") as fh:
        fh.seek(start)
        while left > 0:
            buf = fh.read(min(CHUNK, left))
            if not buf: break
            try: h.wfile.write(buf)
            except (BrokenPipeError, ConnectionResetError): return
            left -= len(buf)


def make_handler(loader):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def log_message(self, *a): pass
        def _send(self, b, ctype, code=200):
            # gzip anything sizeable when the client accepts it: the 360 KB payload + 250 KB peaks were the phone's whole wait
            enc = None
            if len(b) > 1024 and "gzip" in (self.headers.get("Accept-Encoding") or ""):
                b = gzip.compress(b, 6); enc = "gzip"
            self.send_response(code); self.send_header("Content-Type", ctype); self.send_header("Vary", "Accept-Encoding")
            if enc: self.send_header("Content-Encoding", enc)
            self.send_header("Content-Length", str(len(b))); self.end_headers()
            if self.command != "HEAD": self.wfile.write(b)
        def _json(self, obj, code=200):
            self._send(json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8", code)
        def _html(self, t):
            self._send(t.encode(), "text/html; charset=utf-8")
        def _read(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        def do_HEAD(self): self.do_GET()
        def do_GET(self):
            cfg = loader()
            u = urlparse(self.path); p = u.path
            if p == "/" or p == "/index.html":
                return self._html(home_html(cfg))
            m = re.match(r"/v/([\w-]+)$", p)
            if m and m.group(1) in cfg:
                return self._html(PAGE.replace("__VID__", m.group(1)))
            m = re.match(r"/media/([\w-]+)$", p)
            if m and m.group(1) in cfg:
                return _serve_range(self, Path(cfg[m.group(1)]["media"]))
            if p == "/api/health":
                return self._json({"ok": True, "root": str(ROOT), "videos": len(cfg), "time": _now()})
            if p == "/api/feed":            # sent comments across all videos; ?since=N skips the first N lines
                q = parse_qs(u.query); since = int((q.get("since") or ["0"])[0])
                lines = FEED.read_text(encoding="utf-8-sig").splitlines() if FEED.exists() else []
                items = [json.loads(x) for x in lines[since:] if x.strip()]
                return self._json({"total": len(lines), "since": since, "items": items})
            if p == "/api/videos":
                return self._json({"videos": [{"id": k, "title": v.get("title", ""), "state_updated": read_state(k).get("updated_at"),
                                               "handoff": f"/api/{k}/handoff"} for k, v in cfg.items()]})
            m = re.match(r"/api/([\w-]+)$", p)
            if m and m.group(1) in cfg:
                return self._json(build_payload(m.group(1), cfg[m.group(1)]))
            m = re.match(r"/api/([\w-]+)/comments$", p)
            if m and m.group(1) in cfg:
                return self._json({"id": m.group(1), "comments": fold_comments(m.group(1), cfg[m.group(1)])})
            m = re.match(r"/api/([\w-]+)/state$", p)
            if m and m.group(1) in cfg:
                return self._json({"id": m.group(1), "state": read_state(m.group(1))})
            m = re.match(r"/api/([\w-]+)/handoff$", p)
            if m and m.group(1) in cfg:
                b = make_handoff(m.group(1), cfg[m.group(1)]).encode("utf-8")
                self.send_response(200); self.send_header("Content-Type", "text/markdown; charset=utf-8")
                self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b); return
            m = re.match(r"/pins/([\w-]+)/(c\d+\.jpg)$", p)
            if m and m.group(1) in cfg:
                sp = REVIEW_DIR / m.group(1) / "pins" / m.group(2)
                if sp.exists():
                    return _serve_range(self, sp)
            if p == "/logo.png" and LOGO.exists():
                return _serve_range(self, LOGO)
            m = re.match(r"/thumbs/([\w-]+)\.jpg$", p)
            if m and m.group(1) in cfg and cfg[m.group(1)].get("sprite"):
                sp = Path(cfg[m.group(1)]["sprite"])
                if sp.exists():
                    return _serve_range(self, sp)
            m = re.match(r"/poster/([\w-]+)$", p)
            if m and m.group(1) in cfg and cfg[m.group(1)].get("poster"):
                sp = Path(cfg[m.group(1)]["poster"])
                if sp.exists():
                    return _serve_range(self, sp)
            m = re.match(r"/peaks/([\w-]+)\.json$", p)
            if m and m.group(1) in cfg and cfg[m.group(1)].get("peaks"):
                sp = Path(cfg[m.group(1)]["peaks"])
                if sp.exists():
                    return self._send(sp.read_bytes(), "application/json; charset=utf-8")
            self._json({"error": "not found", "path": p}, 404)
        def do_POST(self):
            cfg = loader()
            p = urlparse(self.path).path
            m = re.match(r"/api/([\w-]+)/comment$", p)
            if m and m.group(1) in cfg:
                try:
                    rec = add_comment(m.group(1), cfg[m.group(1)], self._read())
                    return self._json({"ok": True, "comment": rec, "comments": read_comments(m.group(1))})
                except (ValueError, KeyError, json.JSONDecodeError) as e:
                    return self._json({"error": str(e)}, 400)
            m = re.match(r"/api/([\w-]+)/send$", p)
            if m and m.group(1) in cfg:
                try:
                    send_comment(m.group(1), cfg[m.group(1)], self._read().get("ref"))
                    return self._json({"ok": True, "comments": read_comments(m.group(1))})
                except (ValueError, KeyError) as e:
                    return self._json({"error": str(e)}, 400)
            m = re.match(r"/api/([\w-]+)/send_all$", p)
            if m and m.group(1) in cfg:
                n = send_all(m.group(1), cfg[m.group(1)])
                return self._json({"ok": True, "sent": n, "comments": read_comments(m.group(1))})
            m = re.match(r"/api/([\w-]+)/resolve$", p)
            if m and m.group(1) in cfg:
                b = self._read()
                with _LOCK:
                    with open(comments_path(m.group(1)), "a", encoding="utf-8", newline="\n") as fh:
                        fh.write(json.dumps({"kind": "resolve", "ref": b.get("ref"), "resolved": bool(b.get("resolved", True)), "at": _now()}, ensure_ascii=False) + "\n")
                return self._json({"ok": True, "comments": read_comments(m.group(1))})
            m = re.match(r"/api/([\w-]+)/delete$", p)
            if m and m.group(1) in cfg:
                b = self._read()
                with _LOCK:
                    with open(comments_path(m.group(1)), "a", encoding="utf-8", newline="\n") as fh:
                        fh.write(json.dumps({"kind": "delete", "ref": b.get("ref"), "at": _now()}, ensure_ascii=False) + "\n")
                return self._json({"ok": True, "comments": read_comments(m.group(1))})
            m = re.match(r"/api/([\w-]+)/complete$", p)
            if m and m.group(1) in cfg:
                b = self._read()
                with _LOCK:
                    with open(comments_path(m.group(1)), "a", encoding="utf-8", newline="\n") as fh:
                        fh.write(json.dumps({"kind": "complete", "ref": b.get("ref"),
                                             "done": bool(b.get("done", True)), "by": b.get("by", "agent"),
                                             "note": b.get("note", ""), "at": _now()}, ensure_ascii=False) + "\n")
                return self._json({"ok": True, "comments": read_comments(m.group(1))})
            m = re.match(r"/api/([\w-]+)/state$", p)
            if m and m.group(1) in cfg:
                try:
                    return self._json({"ok": True, "state": write_state(m.group(1), self._read())})
                except (ValueError, json.JSONDecodeError) as e:
                    return self._json({"error": str(e)}, 400)
            self._json({"error": "not found", "path": p}, 404)
    return H


class ConfigLoader:
    """Re-reads notecut.json whenever its mtime changes, so `notecut add` is live without a restart."""

    def __init__(self):
        self._mtime = None; self._cfg = {}

    def __call__(self):
        p = config_path()
        try:
            mt = p.stat().st_mtime_ns
        except FileNotFoundError:
            mt = None
        if mt != self._mtime:
            self._cfg = load_config(); self._mtime = mt
        return self._cfg


def serve(host="0.0.0.0", port=8808, root=None):
    set_root(root or ROOT)
    REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    if not config_path().exists():
        save_config({"videos": {}})
    cfg = load_config()
    for k, v in cfg.items():
        if not Path(v["media"]).exists():
            print(f"WARN media missing for {k}: {v['media']}", flush=True)
    httpd = ThreadingHTTPServer((host, port), make_handler(ConfigLoader()))
    httpd.daemon_threads = True
    print(f"Note Cut on http://{host}:{port}/  root={ROOT}  ({len(cfg)} video(s))", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


PAGE = _static("player.html")

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Note Cut server")
    ap.add_argument("--root", default=os.environ.get("NOTECUT_ROOT", "."))
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8808)
    a = ap.parse_args()
    serve(a.host, a.port, a.root)

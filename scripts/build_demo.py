"""Build the bundled demo project (notecut/examples/demo) from real footage.

Dev-only. Needs: ffmpeg, faster-whisper (CUDA). The shipped repo carries the OUTPUT of this script, so `notecut demo`
needs none of those. The footage is the opening bridge scene of "Tears of Steel" (Blender Foundation, mango.blender.org),
CC-BY 3.0 - real actors, real dialogue, and a stammer that a first pass would cut. Downloaded once into .demo_build/.

  python scripts/build_demo.py [--footage tears_of_steel_720p.mov] [--in 21.0] [--dur 42.0]
"""
from __future__ import annotations
import argparse, json, shutil, subprocess, sys, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))
from notecut import prep, server as S  # noqa: E402

OUT = REPO / "notecut" / "examples" / "demo"
FOOTAGE_URL = "https://archive.org/download/Tears-of-Steel/tears_of_steel_720p.mov"   # 355 MB, CC-BY 3.0
CREDIT = "Tears of Steel (c) Blender Foundation | mango.blender.org | CC-BY 3.0"
W = 960

# What the "editor" removes on the first pass: one stammer (located by its first words) and every stretch of dead air
# longer than DEAD_AIR_S, trimmed down to a HEAD/TAIL of silence so the cut breathes.
STAMMER = "I'm not freaked out by"
DEAD_AIR_S, TAIL_S, HEAD_S = 2.5, 0.7, 0.45


def fetch_footage(dst):
    if dst.exists() and dst.stat().st_size > 100_000_000:
        return dst
    print(f"downloading {FOOTAGE_URL} -> {dst} ...", flush=True)
    req = urllib.request.Request(FOOTAGE_URL, headers={"User-Agent": "notecut-build-demo"})
    with urllib.request.urlopen(req) as r, open(dst, "wb") as fh:
        shutil.copyfileobj(r, fh, 1 << 20)
    return dst


def extract(footage, t_in, dur, dst, webm=None):
    """The demo source: a re-encoded excerpt, metadata stripped, moov first (the shipped file must play anywhere)."""
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{t_in:.3f}", "-i", str(footage), "-t", f"{dur:.3f}",
                    "-vf", f"scale={W}:-2", "-c:v", "libx264", "-preset", "slow", "-crf", "22", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-b:a", "128k", "-ac", "2", "-ar", "48000", "-map_metadata", "-1", "-movflags", "+faststart", str(dst)], check=True)
    if webm:   # VP9 copy for headless-Chrome screenshots (headless has no H.264 decoder)
        subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(dst), "-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "34",
                        "-c:a", "libopus", "-b:a", "64k", str(webm)], check=True)


def transcribe(media):
    from faster_whisper import WhisperModel
    m = WhisperModel("large-v3-turbo", device="cuda", compute_type="float16")
    segs, _ = m.transcribe(str(media), word_timestamps=True, language="en", vad_filter=False)
    words = []
    for sg in segs:
        for w in sg.words or []:
            words.append({"w": w.word.strip(), "s": round(w.start, 3), "e": round(w.end, 3), "p": round(w.probability, 3)})
    return words


def norm(s):
    return "".join(ch for ch in s.lower() if ch.isalnum())


def find_phrase(words, phrase, start=0):
    """index range [i, j] of `phrase` inside the ASR words, tolerant to punctuation; None if not found."""
    toks = [norm(t) for t in phrase.split() if norm(t)]
    for i in range(start, len(words) - len(toks) + 1):
        if all(norm(words[i + k]["w"]).startswith(toks[k][:4]) for k in range(len(toks))):
            return i, i + len(toks) - 1
    return None


def cut_ranges(words, duration):
    """Source-time keep ranges: everything except the stammer and the middle of every long silence.
    Cut points land between words (mid-gap), never inside one."""
    removed = []
    hit = find_phrase(words, STAMMER)
    if hit is None:
        raise SystemExit(f"could not locate the stammer in ASR: {STAMMER!r}")
    i, j = hit[0], hit[1]
    while j + 1 < len(words) and words[j + 1]["w"].rstrip(".").lower() in ("it's", "its", "..."):   # the trailing "It's..."
        j += 1
    removed.append(("stammer", (words[i - 1]["e"] + words[i]["s"]) / 2, (words[j]["e"] + words[j + 1]["s"]) / 2))
    for k in range(len(words) - 1):
        gap = words[k + 1]["s"] - words[k]["e"]
        if gap > DEAD_AIR_S and not (i <= k < j):
            removed.append(("dead air", words[k]["e"] + TAIL_S, words[k + 1]["s"] - HEAD_S))
    removed.sort(key=lambda r: r[1])
    keep, cur = [], 0.0
    for _, a, b in removed:
        keep.append({"start": round(cur, 3), "end": round(a, 3)}); cur = b
    keep.append({"start": round(cur, 3), "end": round(min(duration, words[-1]["e"] + 0.6), 3)})
    return keep, removed


def seed_data(words, keep, prior_keep, removed, t_in, dur):
    """A realistic ledger + state so the demo shows every status (saved / sent / done), a pin, and a handoff."""
    S.set_root(OUT)
    d = OUT / "data" / "demo"; d.mkdir(parents=True, exist_ok=True)
    (d / "pins").mkdir(exist_ok=True)
    # c0001: transcript selection on the last line, sent, done by the agent (extended 0.6 s -> v2).
    # c0002: point comment, sent (open). c0003: a saved draft. c0004: a pin on a frame, sent (open).
    hit = find_phrase(words, "this is pretty freaky")
    i0, i1 = hit if hit else (len(words) - 4, len(words) - 1)
    hit2 = find_phrase(words, "having nightmares")
    p0 = hit2[0] if hit2 else 50
    hit3 = find_phrase(words, "robot hand")
    p1 = hit3[1] if hit3 else 37
    segs, off = [], 0.0
    for k in keep:
        segs.append((k["start"], k["end"], off)); off += k["end"] - k["start"]

    def clip_t(src):
        for s, e, o in segs:
            if s <= src <= e:
                return round(o + src - s, 3)
        return 0.0
    recs = [
        {"kind": "transcript", "i0": i0, "i1": i1, "src_start": words[i0]["s"], "src_end": words[i1]["e"],
         "sel": " ".join(w["w"] for w in words[i0:i1 + 1]), "body": "Hold on this line a beat longer before the cut.",
         "id": "c0001", "video": "demo", "sent": True, "at": "2026-09-06T18:02:11Z"},
        {"kind": "point", "clip_t": clip_t(words[p0]["s"]), "body": "Score swells under this line - duck it 3 dB so the last word is clear.",
         "id": "c0002", "video": "demo", "sent": True, "at": "2026-09-06T18:03:40Z"},
        {"kind": "point", "clip_t": 2.0, "body": "Maybe open on 'Look, Celia' and lose the jerk line? (thinking out loud)",
         "id": "c0003", "video": "demo", "sent": False, "at": "2026-09-06T18:05:02Z"},
        {"kind": "pin", "clip_t": clip_t(words[p1]["s"] - 0.3), "x": 0.43, "y": 0.35, "shot": "pins/c0004.jpg",
         "body": "The hand is the payoff for the line - hold this frame a beat before she pulls it back.",
         "id": "c0004", "video": "demo", "sent": True, "at": "2026-09-06T18:06:30Z"},
        {"kind": "complete", "ref": "c0001", "done": True, "by": "editor-agent", "note": "extended 0.6 s; re-rendered v2", "at": "2026-09-06T19:10:00Z"},
    ]
    with open(d / "comments.jsonl", "w", encoding="utf-8", newline="\n") as fh:
        for r in recs:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    with open(OUT / "data" / "feed.jsonl", "w", encoding="utf-8", newline="\n") as fh:
        for r in recs[:1] + recs[1:2] + recs[3:4]:
            fh.write(json.dumps({"at": r["at"], "id": r["id"], "video": "demo", "summary": S.make_summary("demo", {"section_start": 0.0}, r), "rec": r}, ensure_ascii=False) + "\n")
    # pin screenshot = the frame at that clip time, from the preview
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", str(recs[3]["clip_t"]), "-i", str(OUT / "assets" / "demo" / "source.mp4"),
                    "-frames:v", "1", "-q:v", "5", str(d / "pins" / "c0004.jpg")], check=True)
    what = ", ".join(f"{n} {a:.1f}-{b:.1f}" for n, a, b in removed)
    state = {
        "summary": f"Demo cut: {dur:.0f} s bridge scene from Tears of Steel. First pass removed a stammer and the dead air between scenes ({len(keep)} keeps). v2 is current (c0001 applied).",
        "resume": ["Read the open comments: `notecut comments demo --open`",
                   "Apply each one to assets/demo/edl.json (source times), re-render the cut, then `notecut complete demo <id> <note>`",
                   "Write what changed back with `notecut state merge demo '{...}'` and `notecut log demo ...`"],
        "open_items": ["AGENT: c0002 duck the score under the 'nightmares' line", "AGENT: c0004 hold on the robot hand",
                       "OWNER: c0003 is a saved draft - do not act until sent"],
        "rules": ["Never change the source file; every render is derived from edl.json",
                  "Cuts land on word boundaries; the transcript decides WHAT is kept",
                  "A comment is done only when its render is on the server and `complete` was posted"],
        "source": {"file": "assets/demo/source.mp4", "duration_s": round(dur, 3),
                   "note": f"{CREDIT}; {t_in:.1f}-{t_in + dur:.1f} s of the 720p release, scaled to {W} px"},
        "transcript": {"file": "assets/demo/words.json", "words": len(words), "model": "large-v3-turbo"},
        "timeline": {"edl": "assets/demo/edl.json", "keep_segments": len(keep), "prior": "assets/demo/prior_edl.json", "removed": what},
        "renders": {"current": {"file": "assets/demo/source.mp4", "version": "v2", "duration_s": round(off, 3), "built_by": "build_demo.py", "date": "2026-09-07"},
                    "lineage": ["v1 = first cut (prior_edl.json)", "v2 = c0001 applied"]},
        "tools": {"rebuild": "python scripts/build_demo.py (dev) / notecut prep demo"},
        "log": ["2026-09-06 v1 cut posted for review", "2026-09-06 c0001 applied -> v2"],
    }
    S.write_state("demo", {"state": state, "by": "editor-agent", "note": "demo seed"})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--footage", default=str(REPO / ".demo_build" / "tears_of_steel_720p.mov"), help="local copy; downloaded from archive.org if missing")
    ap.add_argument("--in", dest="t_in", type=float, default=21.0, help="excerpt start in the film (s)")
    ap.add_argument("--dur", type=float, default=42.0)
    a = ap.parse_args()
    tmp = REPO / ".demo_build"; tmp.mkdir(exist_ok=True)
    footage = fetch_footage(Path(a.footage))
    if OUT.exists():
        shutil.rmtree(OUT)
    (OUT / "assets" / "demo").mkdir(parents=True); (OUT / "data").mkdir(parents=True)

    src = OUT / "assets" / "demo" / "source.mp4"
    print("extract ...", flush=True)
    extract(footage, a.t_in, a.dur, src, webm=tmp / "source.webm")
    info = prep.probe(src)
    print(f"  {info['duration']:.2f} s {info['width']}x{info['height']}")
    print("asr ...", flush=True)
    words = transcribe(src)
    print(f"  {len(words)} words")
    keep, removed = cut_ranges(words, info["duration"])
    # prior cut: same, but the last line was not yet extended (ends 0.6 s earlier) - shows cur_t remapping
    prior = json.loads(json.dumps(keep)); prior[-1]["end"] = round(prior[-1]["end"] - 0.6, 3)
    json.dump({"source": "assets/demo/source.mp4", "credit": CREDIT, "model": "large-v3-turbo", "language": "en", "words": words},
              open(OUT / "assets" / "demo" / "words.json", "w", encoding="utf-8", newline="\n"), ensure_ascii=False, indent=0)
    json.dump({"keep": keep, "run": {"params": {"ordered": False}, "note": "v2: stammer + dead air removed, last line extended"}},
              open(OUT / "assets" / "demo" / "edl.json", "w", encoding="utf-8", newline="\n"), indent=1)
    json.dump({"keep": prior, "run": {"note": "v1: first cut"}},
              open(OUT / "assets" / "demo" / "prior_edl.json", "w", encoding="utf-8", newline="\n"), indent=1)
    print("prep ...", flush=True)
    # the excerpt is already a small faststart h264, so it is served as-is: no preview.mp4 to ship twice
    frag, info = prep.prepare(src, OUT / "assets" / "demo", proxy=False)
    # home-page poster: a frame with faces in it, not the establishing shot at 0 s
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", "12.6", "-i", str(src), "-frames:v", "1", "-vf", "scale=640:-2", "-q:v", "4",
                    str(OUT / "assets" / "demo" / "poster.jpg")], check=True)
    doc = {"videos": {"demo": {
        "title": "Tears of Steel - bridge scene (demo, v2)",
        "group": "Demo", "source": "assets/demo/source.mp4", "credit": CREDIT,
        "media": "assets/demo/source.mp4", "sprite": "assets/demo/thumbs.jpg", "peaks": "assets/demo/peaks.json", "poster": "assets/demo/poster.jpg",
        "transcript": "assets/demo/words.json", "edl": "assets/demo/edl.json", "prior_edl": "assets/demo/prior_edl.json",
        "section_start": 0.0, "margin_words": 100, "duration": frag["duration"],
        "sprite_interval": frag["sprite_interval"], "sprite_cols": frag["sprite_cols"], "sprite_tw": frag["sprite_tw"], "sprite_th": frag["sprite_th"],
    }}}
    S.set_root(OUT); S.save_config(doc, OUT)
    print("seed ...", flush=True)
    seed_data(words, keep, prior, removed, a.t_in, info["duration"])
    (OUT / "CREDITS.md").write_text(f"Demo footage: {CREDIT}\nhttps://mango.blender.org/ - excerpt {a.t_in:.1f}-{a.t_in + a.dur:.1f} s of the film, rescaled and re-encoded.\n",
                                    encoding="utf-8", newline="\n")
    sizes = {p.name: p.stat().st_size for p in (OUT / "assets" / "demo").iterdir()}
    print(json.dumps({"keep": keep, "removed": removed, "sizes": sizes}, indent=1))


if __name__ == "__main__":
    main()

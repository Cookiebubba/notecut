"""Build the bundled demo project (notecut/examples/demo) from scratch.

Dev-only. Needs: kokoro-onnx (+ model files), faster-whisper, Pillow, numpy, ffmpeg. The shipped repo carries the
OUTPUT of this script so `notecut demo` needs none of those. Everything in the demo is synthetic: a TTS voice
reads a short review-style script, the picture is a generated audio-reactive scene with live captions.

  python scripts/build_demo.py [--voice am_michael] [--model-dir ~/.buzz/models/kokoro]
"""
from __future__ import annotations
import argparse, json, math, os, shutil, subprocess, sys, wave
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))
from notecut import prep, server as S  # noqa: E402

OUT = REPO / "notecut" / "examples" / "demo"
W, H, FPS = 960, 540, 24

# The script: three beats, two deliberate flubs that the "editor" removes. Word timings come from ASR afterwards.
SCRIPT = [
    ("keep", "Okay so I spent a week editing with an AI agent, and here's the thing nobody tells you."),
    ("keep", "The cut itself is fine. The problem is telling it what to change without typing an essay."),
    ("cut",  "Uh, hang on, let me start that again."),
    ("keep", "So we built a page. You watch the cut, you drag across the transcript, and that selection becomes an instruction."),
    ("keep", "Extend this to here. Trim that. Drop a pin on the frame where the logo is wrong."),
    ("cut",  "Wait, is the mic even on? Okay it is."),
    ("keep", "Then you press send, and the agent picks it up with the exact timestamps, not a guess."),
    ("keep", "When it's done, the comment turns green, and the state of the edit lives on the server, not in a chat window."),
    ("keep", "That's the whole idea. Review like a human, hand off like a machine."),
]


def tts(model_dir, voice):
    from kokoro_onnx import Kokoro
    k = Kokoro(str(model_dir / "kokoro-v1.0.onnx"), str(model_dir / "voices-v1.0.bin"))
    chunks, sr = [], 24000
    for _, line in SCRIPT:
        a, sr = k.create(line, voice=voice, speed=1.0, lang="en-us")
        chunks.append(a.astype(np.float32)); chunks.append(np.zeros(int(sr * 0.45), np.float32))
    audio = np.concatenate(chunks)
    audio = audio / max(1e-6, np.abs(audio).max()) * 0.85
    return audio, sr


def write_wav(path, audio, sr):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes((audio * 32767).astype(np.int16).tobytes())


def transcribe(wav):
    from faster_whisper import WhisperModel
    m = WhisperModel("large-v3-turbo", device="cuda", compute_type="float16")
    segs, _ = m.transcribe(str(wav), word_timestamps=True, language="en", vad_filter=False)
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


def cut_ranges(words):
    """Source-time keep ranges = everything except the two 'cut' lines (located by their opening words)."""
    drops, pos = [], 0
    for kind, line in SCRIPT:
        head = " ".join(line.split()[:4])
        hit = find_phrase(words, head, pos)
        if hit is None:
            raise SystemExit(f"could not locate line in ASR: {line!r}")
        i, _ = hit
        # the line ends where the next line starts, or at the last word
        nxt = None
        rest = [l for _, l in SCRIPT[SCRIPT.index((kind, line)) + 1:]]
        if rest:
            h2 = find_phrase(words, " ".join(rest[0].split()[:4]), i + 1)
            nxt = h2[0] if h2 else None
        j = (nxt - 1) if nxt is not None else len(words) - 1
        if kind == "cut":
            drops.append((i, j))
        pos = i + 1
    keep, cur = [], 0.0
    for i, j in drops:
        a = (words[i - 1]["e"] + words[i]["s"]) / 2 if i > 0 else words[i]["s"]
        b = (words[j]["e"] + words[j + 1]["s"]) / 2 if j + 1 < len(words) else words[j]["e"]
        keep.append({"start": round(cur, 3), "end": round(a, 3)}); cur = b
    keep.append({"start": round(cur, 3), "end": round(words[-1]["e"] + 0.6, 3)})
    return keep, drops


def render_frames(audio, sr, words, tmpdir):
    """Audio-reactive monochrome scene + live caption of the current word. Frames go to tmpdir/f%05d.png."""
    font_big = ImageFont.truetype("C:/Windows/Fonts/segoeuib.ttf" if os.name == "nt" else "DejaVuSans-Bold.ttf", 34)
    font_small = ImageFont.truetype("C:/Windows/Fonts/segoeui.ttf" if os.name == "nt" else "DejaVuSans.ttf", 18)
    n = int(math.ceil(len(audio) / sr * FPS))
    env = np.abs(audio)
    hop = sr // FPS
    rms = np.array([np.sqrt((audio[i * hop:(i + 1) * hop] ** 2).mean()) if len(audio[i * hop:(i + 1) * hop]) else 0 for i in range(n)])
    rms = rms / max(1e-6, rms.max())
    sm = np.convolve(rms, np.ones(3) / 3, mode="same")
    tmpdir.mkdir(parents=True, exist_ok=True)
    wi = 0
    for f in range(n):
        t = f / FPS
        img = Image.new("RGB", (W, H), (22, 23, 26))
        d = ImageDraw.Draw(img)
        # subtle grid
        for x in range(0, W, 60):
            d.line([(x, 0), (x, H)], fill=(30, 31, 35))
        for y in range(0, H, 60):
            d.line([(0, y), (W, y)], fill=(30, 31, 35))
        # the "speaker": concentric rings breathing with the voice
        cx, cy = W // 2, H // 2 - 30
        r0 = 70 + 55 * float(sm[f])
        for k, alpha in ((2.2, 40), (1.6, 70), (1.0, 235)):
            r = r0 * k
            col = (alpha, alpha, alpha + 4)
            d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=col, width=2 if k > 1 else 4)
        d.ellipse([cx - 14, cy - 14, cx + 14, cy + 14], fill=(235, 235, 232))
        # side bars = a fake level meter from the recent envelope
        for i in range(24):
            j = f - (23 - i)
            v = float(sm[j]) if 0 <= j < n else 0.0
            hgt = int(6 + v * 90)
            d.rectangle([80 + i * 10, cy + 150 - hgt, 86 + i * 10, cy + 150], fill=(90, 92, 98))
            d.rectangle([W - 80 - i * 10 - 6, cy + 150 - hgt, W - 80 - i * 10, cy + 150], fill=(90, 92, 98))
        # caption: the word being spoken, plus its neighbours faded
        while wi + 1 < len(words) and words[wi + 1]["s"] <= t:
            wi += 1
        cur = words[wi] if words and words[wi]["s"] <= t <= words[wi]["e"] + 0.15 else None
        if cur:
            lo, hi = max(0, wi - 3), min(len(words), wi + 4)
            parts = [(w["w"], j == wi) for j, w in enumerate(words[lo:hi], lo)]
            widths = [d.textlength(p + " ", font=font_big if on else font_small) for p, on in parts]
            x = (W - sum(widths)) / 2
            for (p, on), wd in zip(parts, widths):
                d.text((x, H - 96 if on else H - 86), p, font=font_big if on else font_small,
                       fill=(240, 240, 238) if on else (120, 122, 128))
                x += wd
        d.text((24, H - 34), "NOTE CUT  ·  demo footage (synthetic)", font=font_small, fill=(90, 92, 98))
        d.text((W - 150, H - 34), f"{int(t // 60)}:{t % 60:05.2f}", font=font_small, fill=(90, 92, 98))
        img.save(tmpdir / f"f{f:05d}.png")
    return n


def mux(tmpdir, wav, dst, webm=None):
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-framerate", str(FPS), "-i", str(tmpdir / "f%05d.png"),
                    "-i", str(wav), "-c:v", "libx264", "-preset", "slow", "-crf", "23", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-b:a", "128k", "-map_metadata", "-1", "-movflags", "+faststart", "-shortest", str(dst)], check=True)
    if webm:   # VP9 copy for headless-Chrome screenshots (headless has no H.264 decoder)
        subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(dst), "-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "34",
                        "-c:a", "libopus", "-b:a", "64k", str(webm)], check=True)


def seed_data(words, keep, prior_keep):
    """A realistic ledger + state so the demo shows every status (saved / sent / done), a pin, and a handoff."""
    S.set_root(OUT)
    d = OUT / "data" / "demo"; d.mkdir(parents=True, exist_ok=True)
    (d / "pins").mkdir(exist_ok=True)
    # comment 1: transcript EDIT selection, sent, done by the agent. comment 2: point comment, sent (open work).
    # comment 3: a saved draft. comment 4: a pin with a screenshot, sent.
    hit = find_phrase(words, "review like a human")
    i0, i1 = hit if hit else (len(words) - 6, len(words) - 1)
    hit2 = find_phrase(words, "drop a pin on the frame")
    p0 = hit2[0] if hit2 else 20
    # clip-time helper on the CURRENT cut
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
         "sel": " ".join(w["w"] for w in words[i0:i1 + 1]), "body": "Hold on this line a beat longer before the end card.",
         "id": "c0001", "video": "demo", "sent": True, "at": "2026-09-06T18:02:11Z"},
        {"kind": "point", "clip_t": clip_t(words[p0]["s"]), "body": "Music bed is too loud under this sentence - duck it 3 dB.",
         "id": "c0002", "video": "demo", "sent": True, "at": "2026-09-06T18:03:40Z"},
        {"kind": "point", "clip_t": 2.0, "body": "Maybe open on the second sentence instead? (thinking out loud)",
         "id": "c0003", "video": "demo", "sent": False, "at": "2026-09-06T18:05:02Z"},
        {"kind": "pin", "clip_t": clip_t(words[max(0, p0 + 5)]["s"]), "x": 0.5, "y": 0.44, "shot": "pins/c0004.jpg",
         "body": "The centre mark drifts up here - keep it on the same line as the meters.",
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
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", str(recs[3]["clip_t"]), "-i", str(OUT / "assets" / "demo" / "preview.mp4"),
                    "-frames:v", "1", "-q:v", "5", str(d / "pins" / "c0004.jpg")], check=True)
    state = {
        "summary": "Demo cut: a 50 s synthetic monologue with two flubs removed. v2 is current (c0001 applied).",
        "resume": ["Read the open comments: `notecut comments demo --open`",
                   "Apply each one to assets/demo/edl.json (source times), re-render preview.mp4, then `notecut complete demo <id> <note>`",
                   "Write what changed back with `notecut state merge demo '{...}'` and `notecut log demo ...`"],
        "open_items": ["AGENT: c0002 duck the bed under the 'drop a pin' sentence", "AGENT: c0004 centre mark alignment",
                       "OWNER: c0003 is a saved draft - do not act until sent"],
        "rules": ["Never change the source file; every render is derived from edl.json",
                  "Cuts land on word boundaries; the transcript decides WHAT is kept",
                  "A comment is done only when its render is on the server and `complete` was posted"],
        "source": {"file": "assets/demo/source.mp4", "duration_s": round(off, 3), "note": "synthetic TTS + generated picture"},
        "transcript": {"file": "assets/demo/words.json", "words": len(words), "model": "large-v3-turbo"},
        "timeline": {"edl": "assets/demo/edl.json", "keep_segments": len(keep), "prior": "assets/demo/prior_edl.json"},
        "renders": {"current": {"file": "assets/demo/source.mp4", "version": "v2", "duration_s": round(off, 3), "built_by": "build_demo.py", "date": "2026-09-06"},
                    "lineage": ["v1 = first cut (prior_edl.json)", "v2 = c0001 applied"]},
        "tools": {"rebuild": "python scripts/build_demo.py (dev) / notecut prep demo"},
        "log": ["2026-09-06 v1 cut posted for review", "2026-09-06 c0001 applied -> v2"],
    }
    S.write_state("demo", {"state": state, "by": "editor-agent", "note": "demo seed"})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--voice", default="am_michael")
    ap.add_argument("--model-dir", default=str(Path.home() / ".buzz" / "models" / "kokoro"))
    ap.add_argument("--keep-tmp", action="store_true")
    a = ap.parse_args()
    tmp = REPO / ".demo_build"; tmp.mkdir(exist_ok=True)
    if OUT.exists():
        shutil.rmtree(OUT)
    (OUT / "assets" / "demo").mkdir(parents=True); (OUT / "data").mkdir(parents=True)

    print("tts ...", flush=True)
    audio, sr = tts(Path(a.model_dir), a.voice)
    wav = tmp / "voice.wav"; write_wav(wav, audio, sr)
    print(f"  {len(audio) / sr:.1f} s")
    print("asr ...", flush=True)
    words = transcribe(wav)
    print(f"  {len(words)} words")
    keep, drops = cut_ranges(words)
    # prior cut: same, but the last sentence was not yet extended (ends 0.6 s earlier) - shows cur_t remapping
    prior = json.loads(json.dumps(keep)); prior[-1]["end"] = round(prior[-1]["end"] - 0.6, 3)
    print("frames ...", flush=True)
    n = render_frames(audio, sr, words, tmp / "frames")
    print(f"  {n} frames")
    src = OUT / "assets" / "demo" / "source.mp4"
    print("mux ...", flush=True)
    mux(tmp / "frames", wav, src, webm=tmp / "source.webm")
    json.dump({"source": "assets/demo/source.mp4", "model": "large-v3-turbo", "language": "en", "words": words},
              open(OUT / "assets" / "demo" / "words.json", "w", encoding="utf-8", newline="\n"), ensure_ascii=False, indent=0)
    json.dump({"keep": keep, "run": {"params": {"ordered": False}, "note": "v2: two flubs removed, last line extended"}},
              open(OUT / "assets" / "demo" / "edl.json", "w", encoding="utf-8", newline="\n"), indent=1)
    json.dump({"keep": prior, "run": {"note": "v1: first cut"}},
              open(OUT / "assets" / "demo" / "prior_edl.json", "w", encoding="utf-8", newline="\n"), indent=1)
    print("prep ...", flush=True)
    frag, info = prep.prepare(src, OUT / "assets" / "demo", height=540)
    doc = {"videos": {"demo": {
        "title": "Review like a human, hand off like a machine (demo, v2)",
        "group": "Demo", "source": "assets/demo/source.mp4",
        "media": "assets/demo/preview.mp4", "sprite": "assets/demo/thumbs.jpg", "peaks": "assets/demo/peaks.json",
        "transcript": "assets/demo/words.json", "edl": "assets/demo/edl.json", "prior_edl": "assets/demo/prior_edl.json",
        "section_start": 0.0, "margin_words": 100, "duration": frag["duration"],
        "sprite_interval": frag["sprite_interval"], "sprite_cols": frag["sprite_cols"], "sprite_tw": frag["sprite_tw"], "sprite_th": frag["sprite_th"],
    }}}
    S.set_root(OUT); S.save_config(doc, OUT)
    print("seed ...", flush=True)
    seed_data(words, keep, prior)
    shutil.copy2(tmp / "source.webm", OUT / "assets" / "demo" / "preview.webm")   # screenshots only; deleted by scripts/screenshots.py
    sizes = {p.name: p.stat().st_size for p in (OUT / "assets" / "demo").iterdir()}
    print(json.dumps({"keep": keep, "drops": drops, "sizes": sizes}, indent=1))
    if not a.keep_tmp:
        shutil.rmtree(tmp / "frames", ignore_errors=True)


if __name__ == "__main__":
    main()

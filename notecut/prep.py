"""Turn a master video into what the viewer serves: a light 720p faststart proxy, a hover sprite sheet,
and a waveform peaks file. ffmpeg/ffprobe on PATH is the only requirement; numpy speeds peaks up when present.
"""
from __future__ import annotations
import json, math, shutil, subprocess
from array import array
from pathlib import Path

SR = 16000


def have_ffmpeg():
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _run(cmd, **kw):
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", **kw)
    if r.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed ({r.returncode}): {r.stderr.strip()[-800:]}")
    return r


def probe(path):
    """{"duration": s, "width": px, "height": px, "fps": float, "audio": bool}"""
    r = _run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)])
    d = json.loads(r.stdout)
    out = {"duration": float(d.get("format", {}).get("duration") or 0.0), "width": None, "height": None, "fps": None, "audio": False}
    for s in d.get("streams", []):
        if s.get("codec_type") == "video" and out["width"] is None:
            out["width"], out["height"] = s.get("width"), s.get("height")
            num, _, den = (s.get("avg_frame_rate") or "0/1").partition("/")
            try:
                out["fps"] = float(num) / float(den or 1)
            except (ValueError, ZeroDivisionError):
                out["fps"] = None
        elif s.get("codec_type") == "audio":
            out["audio"] = True
    return out


def make_proxy(src, dst, height=720, crf=24):
    """720p H.264 + AAC, metadata stripped, moov first so playback starts before the whole file arrives."""
    _run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(src),
          "-vf", f"scale=-2:{height}", "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf), "-pix_fmt", "yuv420p",
          "-c:a", "aac", "-b:a", "160k", "-map_metadata", "-1", "-movflags", "+faststart", str(dst)])
    return dst


def make_sprite(src, dst, duration, interval=4, cols=20, tw=160, th=90):
    """One JPEG: a tile every `interval` seconds, `cols` per row. The viewer shows the tile under the cursor."""
    rows = max(1, math.ceil(math.ceil(duration / interval) / cols))
    _run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(src),
          "-vf", f"fps=1/{interval},scale={tw}:{th},tile={cols}x{rows}", "-frames:v", "1", "-q:v", "4", str(dst)])
    return {"sprite_interval": interval, "sprite_cols": cols, "sprite_tw": tw, "sprite_th": th}


def make_peaks(src, dst, hz=50):
    """{"hz", "n", "peak"[uint8], "rms"[uint8]} at `hz` frames per second, normalised to the 99.5th percentile peak."""
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", str(src), "-vn", "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"],
                       capture_output=True)
    if r.returncode != 0:
        raise RuntimeError("ffmpeg failed decoding audio: " + r.stderr.decode("utf-8", "replace")[-400:])
    pcm = array("h"); pcm.frombytes(r.stdout[: len(r.stdout) // 2 * 2])
    step = SR // hz
    n = len(pcm) // step
    try:
        import numpy as np
        x = np.frombuffer(pcm.tobytes(), dtype=np.int16).astype(np.float32)[: n * step].reshape(n, step) / 32768.0
        peak = np.abs(x).max(axis=1); rms = np.sqrt((x * x).mean(axis=1))
        ref = max(1e-6, float(np.percentile(peak, 99.5)))
        q = lambda a: np.clip(np.round(a / ref * 255), 0, 255).astype(np.uint8).tolist()
        pk, rm = q(peak), q(rms)
    except ImportError:
        peak, rms = [], []
        for i in range(n):
            fr = pcm[i * step:(i + 1) * step]
            mx = 0; acc = 0
            for v in fr:
                a = -v if v < 0 else v
                if a > mx: mx = a
                acc += v * v
            peak.append(mx / 32768.0); rms.append(math.sqrt(acc / step) / 32768.0)
        srt = sorted(peak); ref = max(1e-6, srt[min(len(srt) - 1, int(len(srt) * 0.995))]) if srt else 1.0
        q = lambda a: [max(0, min(255, int(round(v / ref * 255)))) for v in a]
        pk, rm = q(peak), q(rms)
    with open(dst, "w", encoding="utf-8", newline="\n") as f:
        json.dump({"hz": hz, "n": n, "peak": pk, "rms": rm}, f, separators=(",", ":"))
    return {"hz": hz, "n": n}


def prepare(src, out_dir, proxy=True, sprite=True, peaks=True, height=720):
    """Build every derived asset for one video into out_dir. Returns the config fragment to merge."""
    src, out_dir = Path(src), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    info = probe(src)
    frag = {"duration": round(info["duration"], 3)}
    if proxy:
        p = out_dir / "preview.mp4"
        # a source that is already <= target height and h264 still gets re-muxed: metadata off, moov first, known-good
        make_proxy(src, p, height=min(height, info["height"] or height))
        frag["media"] = str(p)
    if sprite:
        s = out_dir / "thumbs.jpg"
        frag.update(make_sprite(frag.get("media", src), s, info["duration"]))
        frag["sprite"] = str(s)
    if peaks and info["audio"]:
        k = out_dir / "peaks.json"
        make_peaks(src, k)
        frag["peaks"] = str(k)
    return frag, info

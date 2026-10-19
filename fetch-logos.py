#!/usr/bin/env python3
"""Build menubar/logos/<key>.png (32x32) for the harnesses the sessions menu shows.
Source order: openbrand (largest square raster) -> Google favicon service -> a local brand file.
Re-run any time; existing files are replaced."""
import io, json, os, sys
from PIL import Image

HERE = os.path.dirname(os.path.realpath(__file__))
OUT = os.path.join(HERE, "logos")
SIZE = 32

# key -> (site, local fallback file or None). Only the two harness marks are shown.
SOURCES = {
    "claude":        ("claude.ai", None),   # the orange Claude mark, not the Anthropic wordmark
    "codex":         ("openai.com", None),
}


def get(url, timeout=20):
    """curl, not urllib: the Python here has no root certificates."""
    import subprocess
    r = subprocess.run(["curl", "-sL", "--max-time", str(timeout), "-A", "Mozilla/5.0 (ai-subs logo cache)", url],
                       capture_output=True, check=False)
    if r.returncode != 0 or not r.stdout:
        raise RuntimeError(f"curl {r.returncode}")
    return r.stdout


def openbrand_candidates(site):
    try:
        d = json.loads(get(f"https://openbrand.sh/api/extract?url=https://{site}"))
    except Exception:
        return []
    logos = (d.get("data") or {}).get("logos") or d.get("logos") or []
    cands = []
    for it in logos:
        url = str(it.get("url", ""))
        if not url.startswith("http") or url.lower().endswith((".svg", ".ico")):
            continue
        res = it.get("resolution") or {}
        w, h = res.get("width") or 0, res.get("height") or 0
        if w and h and abs(w - h) > max(w, h) * 0.15:
            continue  # not square
        cands.append((min(w, h) or 1, url))
    return [u for _, u in sorted(cands, reverse=True)]


def to_png(data):
    im = Image.open(io.BytesIO(data)).convert("RGBA")
    w, h = im.size
    s = min(w, h)
    im = im.crop(((w - s) // 2, (h - s) // 2, (w - s) // 2 + s, (h - s) // 2 + s)).resize((SIZE, SIZE), Image.LANCZOS)
    # round the corners a little so it reads as an app icon in the menu
    mask = Image.new("L", (SIZE, SIZE), 0)
    from PIL import ImageDraw
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, SIZE - 1, SIZE - 1), radius=7, fill=255)
    if im.getchannel("A").getextrema() == (255, 255):   # opaque square: round its corners
        im.putalpha(mask)
    buf = io.BytesIO(); im.save(buf, "PNG"); return buf.getvalue()


def main():
    os.makedirs(OUT, exist_ok=True)
    for key, (site, local) in SOURCES.items():
        src = None; data = None
        for url in openbrand_candidates(site):
            try:
                data = get(url); src = f"openbrand {url}"
                Image.open(io.BytesIO(data)).verify()   # make sure it parses before we commit to it
                break
            except Exception:
                data = None
        if not data:
            try:
                data = get(f"https://www.google.com/s2/favicons?domain={site}&sz=128"); src = "google favicon"
                if len(data) < 500: data = None
            except Exception: data = None
        if not data and local and os.path.isfile(local) and not local.endswith(".svg"):
            data = open(local, "rb").read(); src = f"local {os.path.basename(local)}"
        if not data:
            print(f"{key:14} MISSING"); continue
        try:
            png = to_png(data)
        except Exception as e:
            print(f"{key:14} unreadable image from {src}: {e}"); continue
        open(os.path.join(OUT, key + ".png"), "wb").write(png)
        print(f"{key:14} ok  <- {src}")


if __name__ == "__main__":
    sys.exit(main())

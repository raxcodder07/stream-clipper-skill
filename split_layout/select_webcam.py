#!/usr/bin/env python3
"""Pick an area on a video frame and save it as JSON.

Usage:
  python3 select_webcam.py VIDEO                 # pick the streamer's WEBCAM
  python3 select_webcam.py VIDEO --kind game     # pick the GAMEPLAY area (no webcam in it)
  options: [--time 60] [--out FILE.json] [--free]

Saves VIDEO.webcam.json or VIDEO.game.json next to the video.
Drag a box, then press Enter (or click Save).
Keys: Enter = save, R = reset, N = show another frame (+60 s), Esc/Q = cancel.
By default the box keeps the same shape as its panel in the final video
(webcam 1080x900, gameplay 1080x930) so nothing gets cropped twice. Use --free for any shape.

Needs only ffmpeg and Python's built-in tkinter (sudo apt install python3-tk).
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile

PANELS = {"webcam": (1080, 900), "game": (1080, 930)}  # panels in the 1080x1920 video


def probe(video):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height:format=duration",
         "-of", "json", video],
        capture_output=True, text=True, check=True).stdout
    d = json.loads(out)
    s = d["streams"][0]
    return int(s["width"]), int(s["height"]), float(d["format"].get("duration") or 0)


def grab(video, t, png, max_w, max_h):
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-ss", str(t), "-i", video,
         "-frames:v", "1", "-vf",
         f"scale=w={max_w}:h={max_h}:force_original_aspect_ratio=decrease", png],
        check=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("video")
    ap.add_argument("--time", type=float, default=None,
                    help="second of the video to show (default: about 1/3 in)")
    ap.add_argument("--out", help="where to save the JSON")
    ap.add_argument("--free", action="store_true", help="allow any box shape")
    ap.add_argument("--kind", choices=["webcam", "game"], default="webcam",
                    help="what you are selecting (default: webcam)")
    a = ap.parse_args()

    video = os.path.abspath(os.path.expanduser(a.video))
    out = a.out or os.path.splitext(video)[0] + f".{a.kind}.json"
    sw, sh, dur = probe(video)

    try:
        import tkinter as tk
    except ImportError:
        sys.exit("tkinter is missing. Install it with: sudo apt install python3-tk")

    root = tk.Tk()
    what = "the streamer's webcam" if a.kind == "webcam" else "the GAMEPLAY area (leave the webcam out)"
    root.title(f"Drag a box around {what}, then press Enter")
    max_w = int(root.winfo_screenwidth() * 0.9)
    max_h = int(root.winfo_screenheight() * 0.78)
    t0 = a.time if a.time is not None else (min(60.0, dur / 3) if dur else 0.0)
    png = os.path.join(tempfile.mkdtemp(), "frame.png")
    st = {"img": None, "scale": 1.0, "rect": None, "start": None, "box": None, "t": t0}
    ratio = PANELS[a.kind][0] / PANELS[a.kind][1]

    canvas = tk.Canvas(root, highlightthickness=0, cursor="crosshair")
    canvas.pack()
    msg = tk.Label(root, text=f"Drag a box around {what}. Enter = save, "
                              "R = reset, N = another frame, Esc = cancel")
    msg.pack(pady=4)

    def load():
        grab(video, st["t"], png, max_w, max_h)
        img = tk.PhotoImage(file=png)
        st["img"] = img
        st["scale"] = sw / img.width()
        canvas.config(width=img.width(), height=img.height())
        canvas.delete("all")
        canvas.create_image(0, 0, anchor="nw", image=img)
        st["rect"], st["box"], st["start"] = None, None, None

    def press(e):
        st["start"] = (e.x, e.y)

    def drag(e):
        if not st["start"]:
            return
        x0, y0 = st["start"]
        W, H = st["img"].width(), st["img"].height()
        dx, dy = e.x - x0, e.y - y0
        sx = 1 if dx >= 0 else -1
        sy = 1 if dy >= 0 else -1
        max_w_ok = x0 if sx < 0 else W - x0
        max_h_ok = y0 if sy < 0 else H - y0
        if a.free:
            w, h = min(abs(dx), max_w_ok), min(abs(dy), max_h_ok)
        else:
            w = min(abs(dx), max_w_ok, max_h_ok * ratio)
            h = w / ratio
        x1, y1 = x0 + sx * w, y0 + sy * h
        st["box"] = (x0, y0, x1, y1)
        if st["rect"]:
            canvas.delete(st["rect"])
        st["rect"] = canvas.create_rectangle(x0, y0, x1, y1, outline="#ff2e93", width=3)

    def save(_=None):
        if not st["box"]:
            msg.config(text="Drag a box first.")
            return
        x0, y0, x1, y1 = st["box"]
        s = st["scale"]
        x, y = int(min(x0, x1) * s) // 2 * 2, int(min(y0, y1) * s) // 2 * 2
        w, h = int(abs(x1 - x0) * s) // 2 * 2, int(abs(y1 - y0) * s) // 2 * 2
        w, h = min(w, sw - x), min(h, sh - y)
        if w < 64 or h < 64:
            msg.config(text="Box too small. Drag a bigger one.")
            return
        with open(out, "w") as f:
            json.dump({"x": x, "y": y, "w": w, "h": h,
                       "source_w": sw, "source_h": sh}, f, indent=2)
        print(f"Saved {a.kind} box x={x} y={y} w={w} h={h} -> {out}")
        root.destroy()

    def reset(_=None):
        canvas.delete(st["rect"] or "none")
        st["rect"], st["box"] = None, None

    def nxt(_=None):
        st["t"] = (st["t"] + 60) % dur if dur else st["t"] + 60
        load()

    def cancel(_=None):
        print("Cancelled, nothing saved.")
        root.destroy()
        sys.exit(1)

    canvas.bind("<ButtonPress-1>", press)
    canvas.bind("<B1-Motion>", drag)
    tk.Button(root, text="Save (Enter)", command=save).pack(pady=4)
    for key, fn in (("<Return>", save), ("r", reset), ("n", nxt),
                    ("<Escape>", cancel), ("q", cancel)):
        root.bind(key, fn)
    load()
    root.mainloop()


if __name__ == "__main__":
    main()

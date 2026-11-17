#!/usr/bin/env python3
"""Make a 1080x1920 split-screen Short from a streamer video.

Layout (top to bottom):
  gameplay / screen content   1080x930
  name banner strip           1080x90   (gradient + streamer name)
  streamer webcam             1080x900
Captions (bold, current word highlighted) sit near the middle and never overlap
each other in time or space.

Usage (run with the venv that has faster-whisper):
  ~/.venvs/clip/bin/python split_clip.py VIDEO --start 125 --end 155 \\
      --name NOUGHT --title "GTA 6 PRE-LOAD IS LIVE" -o ~/clips/out/clip1.mp4

LANGUAGES
  --lang hi          language SPOKEN in the video (Whisper code: en, hi, es, ar, ja ...).
                     Default: auto-detect. Captions come out in that language's script.
  --hinglish         Hindi speech written in English letters (Hinglish).
  Captions in a DIFFERENT language than the speech (translation, or Hinglish
  from Devanagari): two steps, so Claude can do the translating:
    1) ... --dump-captions ~/clips/out/clip1.captions.json     (writes timed text, no render)
    2) Claude rewrites the "text" fields in the target language, then:
       ... --captions-json ~/clips/out/clip1.captions.json -o ~/clips/out/clip1.mp4
  Non-Latin scripts need a font: sudo apt install fonts-noto-core fonts-noto-cjk

The webcam box comes from VIDEO.webcam.json (select_webcam.py). The gameplay box
comes from VIDEO.game.json if it exists. The webcam is blurred inside the gameplay
panel (--cover-cam) so it only appears once.
"""
import argparse
import json
import os
import subprocess
import tempfile

W, H = 1080, 1920
TOP_H, BAND_H, CAM_H = 930, 90, 900
HIGHLIGHT = "&H0000FFFF&"   # yellow (ASS colours are BGR)
WHITE = "&H00FFFFFF&"
HINGLISH_PROMPT = ("Haan bhai, aaj hum GTA 6 ke baare mein baat karenge. "
                   "Ye bahut crazy hai yaar, kya scene hai, mujhe toh samajh nahi aa raha!")

# name, first code point, last code point, preferred installed font families
SCRIPTS = [
    ("devanagari", 0x0900, 0x097F, ["Noto Sans Devanagari", "Mukta", "Lohit Devanagari"]),
    ("bengali", 0x0980, 0x09FF, ["Noto Sans Bengali"]),
    ("gurmukhi", 0x0A00, 0x0A7F, ["Noto Sans Gurmukhi"]),
    ("gujarati", 0x0A80, 0x0AFF, ["Noto Sans Gujarati"]),
    ("tamil", 0x0B80, 0x0BFF, ["Noto Sans Tamil"]),
    ("telugu", 0x0C00, 0x0C7F, ["Noto Sans Telugu"]),
    ("kannada", 0x0C80, 0x0CFF, ["Noto Sans Kannada"]),
    ("malayalam", 0x0D00, 0x0D7F, ["Noto Sans Malayalam"]),
    ("thai", 0x0E00, 0x0E7F, ["Noto Sans Thai"]),
    ("arabic", 0x0600, 0x06FF, ["Noto Sans Arabic", "Noto Naskh Arabic"]),
    ("hebrew", 0x0590, 0x05FF, ["Noto Sans Hebrew"]),
    ("kana", 0x3040, 0x30FF, ["Noto Sans CJK JP"]),
    ("cjk", 0x4E00, 0x9FFF, ["Noto Sans CJK JP", "Noto Sans CJK SC"]),
    ("hangul", 0xAC00, 0xD7AF, ["Noto Sans CJK KR"]),
    ("cyrillic", 0x0400, 0x04FF, ["DejaVu Sans"]),
]
WIDTH_FACTOR = {"latin": 0.60, "cyrillic": 0.62, "cjk": 1.0, "kana": 1.0, "hangul": 1.0}
SPACELESS = ("cjk", "kana", "thai")


# ---------------------------------------------------------------- helpers
def ts(t):
    cs = int(round(max(0.0, t) * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def esc(text):
    return (text.replace("\\", "").replace("{", "(").replace("}", ")")
            .replace("\n", " ").strip())


def detect_script(text):
    counts = {}
    for ch in text:
        o = ord(ch)
        for name, lo, hi, _ in SCRIPTS:
            if lo <= o <= hi:
                counts[name] = counts.get(name, 0) + 1
                break
        else:
            if ch.isalpha():
                counts["latin"] = counts.get("latin", 0) + 1
    return max(counts, key=counts.get) if counts else "latin"


def pick_font(user_font, script):
    if user_font:
        return user_font
    if script in ("latin", "cyrillic"):
        return "DejaVu Sans"
    try:
        fams = subprocess.run(["fc-list", ":", "family"], capture_output=True,
                              text=True).stdout.lower()
    except FileNotFoundError:
        fams = ""
    for name, _, _, prefs in SCRIPTS:
        if name == script:
            for p in prefs:
                if p.lower() in fams:
                    return p
    print(f"Warning: no font found for {script} text, letters may show as boxes. "
          "Install: sudo apt install fonts-noto-core fonts-noto-cjk")
    return "Noto Sans"


def limits(script, size):
    """Max characters and words per caption line so it never wraps onto 2 lines."""
    factor = WIDTH_FACTOR.get(script, 0.65)
    max_chars = max(5, int(960 / (size * factor)))
    max_words = 6 if script in SPACELESS else 3
    return max_chars, max_words


def chunk_words(words, max_words, max_chars, max_gap=0.6):
    chunks, cur, cur_len = [], [], 0
    for w in words:
        wl = len(w["word"])
        gap = bool(cur) and (w["start"] - cur[-1]["end"] > max_gap)
        if cur and (len(cur) >= max_words or cur_len + 1 + wl > max_chars or gap):
            chunks.append(cur)
            cur, cur_len = [], 0
        cur.append(w)
        cur_len += wl + (1 if len(cur) > 1 else 0)
        if w["word"].rstrip()[-1:] in ".?!\u3002\uff01\uff1f":
            chunks.append(cur)
            cur, cur_len = [], 0
    if cur:
        chunks.append(cur)
    return chunks


def events_from_chunks(chunks):
    ev = []
    for chunk in chunks:
        for i, w in enumerate(chunk):
            start = w["start"]
            end = chunk[i + 1]["start"] if i + 1 < len(chunk) else w["end"] + 0.15
            ev.append({"start": start, "end": max(end, start + 0.06),
                       "words": [c["word"] for c in chunk], "hi": i})
    return ev


def events_from_caption(start, end, text, max_chars):
    """Turn one timed caption (text rewritten by Claude) into highlight events."""
    words = text.split()
    if not words:
        return []
    groups, cur, cl = [], [], 0
    for w in words:
        if cur and cl + 1 + len(w) > max_chars:
            groups.append(cur)
            cur, cl = [], 0
        cur.append(w)
        cl += len(w) + (1 if len(cur) > 1 else 0)
    if cur:
        groups.append(cur)
    total = sum(len(" ".join(g)) for g in groups) or 1
    ev, t = [], start
    for g in groups:
        gdur = (end - start) * len(" ".join(g)) / total
        wt = sum(len(x) + 1 for x in g)
        ws = t
        for i, x in enumerate(g):
            wd = gdur * (len(x) + 1) / wt
            ev.append({"start": ws, "end": ws + wd, "words": g,
                       "hi": i if len(g) > 1 else -1})
            ws += wd
        t += gdur
    return ev


def fix_overlaps(ev, dur):
    """Guarantee that no two caption events are on screen at the same time."""
    ev = sorted(ev, key=lambda e: (e["start"], e["end"]))
    for i in range(len(ev) - 1):
        if ev[i]["end"] > ev[i + 1]["start"] - 0.01:
            ev[i]["end"] = ev[i + 1]["start"] - 0.01
    out = []
    for e in ev:
        e["start"], e["end"] = max(0.0, e["start"]), min(dur, e["end"])
        if e["end"] - e["start"] >= 0.03:
            out.append(e)
    return out


def build_ass(events, dur, cap_font, ui_font, cap_size, cap_y, name, title,
              watermark, upper):
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
Style: Cap,{cap_font},{cap_size},{WHITE},&H000000FF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,8,0,5,40,40,0,1
Style: Title,{ui_font},50,&H00932EFF,&H000000FF,&H00FFFFFF,&H00FFFFFF,-1,0,0,0,100,100,0,0,3,20,0,8,40,40,60,1
Style: Name,{ui_font},54,&H00101010,&H000000FF,&H00000000,&H00000000,-1,0,0,0,100,100,2,0,1,0,0,5,0,0,0,1
Style: Mark,{ui_font},26,{WHITE},&H000000FF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,2,0,6,0,0,0,1

[Events]
Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
"""
    lines = []
    end_all = ts(dur)
    band_mid = TOP_H + BAND_H // 2
    if title:
        lines.append(f"Dialogue: 1,{ts(0)},{end_all},Title,,0,0,0,,{esc(title).upper()}")
    if name:
        lines.append(f"Dialogue: 1,{ts(0)},{end_all},Name,,0,0,0,,"
                     f"{{\\an5\\pos({W // 2},{band_mid})}}{esc(name).upper()}")
    if watermark:
        lines.append(f"Dialogue: 1,{ts(0)},{end_all},Mark,,0,0,0,,"
                     f"{{\\an6\\pos({W - 30},{TOP_H - 25})}}{esc(watermark).upper()}")
    for e in events:
        parts = []
        for j, word in enumerate(e["words"]):
            word = esc(word)
            if upper:
                word = word.upper()
            if word:
                parts.append(f"{{\\c{HIGHLIGHT if j == e['hi'] else WHITE}}}{word}")
        if parts:
            lines.append(f"Dialogue: 2,{ts(e['start'])},{ts(e['end'])},Cap,,0,0,0,,"
                         f"{{\\an5\\pos({W // 2},{cap_y})}}" + " ".join(parts))
    return head + "\n".join(lines) + "\n"


def get_words(video, start, dur, model_name, tmp, lang, hinglish):
    """Return (words, detected_language). Word times are relative to the clip start."""
    base = os.path.splitext(video)[0]
    side, side_lang = base + ".words.json", base + ".words.lang"
    cached_lang = None
    if os.path.exists(side_lang):
        with open(side_lang) as f:
            cached_lang = f.read().strip() or None
    cache_ok = (not hinglish and os.path.exists(side) and
                (lang is None or (cached_lang is not None and cached_lang == lang)))
    if cache_ok:
        with open(side) as f:
            allw = json.load(f)
        out = [{"start": max(0.0, w["start"] - start), "end": min(dur, w["end"] - start),
                "word": w["word"]} for w in allw
               if w["end"] > start and w["start"] < start + dur]
        return out, cached_lang
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise SystemExit("faster-whisper not found. Run this script with the venv "
                         "python: ~/.venvs/clip/bin/python split_clip.py ...")
    wav = os.path.join(tmp, "clip.wav")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}",
                    "-i", video, "-vn", "-ac", "1", "-ar", "16000", wav], check=True)
    model = WhisperModel(model_name, device="cpu", compute_type="int8")
    segments, info = model.transcribe(
        wav, word_timestamps=True, vad_filter=True, beam_size=1,
        language=("hi" if hinglish else lang),
        initial_prompt=(HINGLISH_PROMPT if hinglish else None))
    words = [{"start": w.start, "end": w.end, "word": w.word.strip()}
             for s in segments for w in (s.words or []) if w.word.strip()]
    return words, getattr(info, "language", lang)


def has_filter(name):
    out = subprocess.run(["ffmpeg", "-hide_banner", "-filters"],
                         capture_output=True, text=True).stdout
    return any(line.split()[1:2] == [name] for line in out.splitlines())


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("video")
    ap.add_argument("--start", type=float, required=True, help="clip start (seconds)")
    ap.add_argument("--end", type=float, required=True, help="clip end (seconds, max 30 s long)")
    ap.add_argument("-o", "--out", help="output .mp4 (not needed with --dump-captions)")
    ap.add_argument("--webcam", help="webcam JSON (default: VIDEO.webcam.json)")
    ap.add_argument("--game", help="crop of the gameplay area as x,y,w,h "
                                   "(default: VIDEO.game.json if it exists, else whole frame)")
    ap.add_argument("--cover-cam", choices=["blur", "black", "none"], default="blur",
                    help="hide the webcam inside the gameplay panel (default: blur)")
    ap.add_argument("--name", default="", help="streamer name for the banner strip")
    ap.add_argument("--title", default="", help="title pill at the top")
    ap.add_argument("--watermark", default="", help="small text above the banner, right side")
    ap.add_argument("--lang", default=None, help="spoken language code (default: auto-detect)")
    ap.add_argument("--hinglish", action="store_true",
                    help="Hindi speech, captions in English letters")
    ap.add_argument("--dump-captions", metavar="JSON",
                    help="write timed caption text to JSON and stop (no render)")
    ap.add_argument("--captions-json", metavar="JSON",
                    help="use captions from this JSON (e.g. translated by Claude)")
    ap.add_argument("--font", default=None, help="caption font (default: chosen by script)")
    ap.add_argument("--ui-font", default="DejaVu Sans", help="font for title/banner")
    ap.add_argument("--size", type=int, default=80, help="caption font size")
    ap.add_argument("--caption-y", type=int, default=1100,
                    help="caption centre, pixels from the top of the 1920 px frame "
                         "(~1100 = just under the banner, ~1560 = low on the webcam)")
    ap.add_argument("--model", default="base",
                    help="whisper model: tiny, base, small (use small for Hindi/other languages)")
    a = ap.parse_args()

    video = os.path.abspath(os.path.expanduser(a.video))
    dur = min(a.end - a.start, 30.0)
    if dur <= 1:
        raise SystemExit("--end must be at least 1 second after --start")
    if not a.out and not a.dump_captions:
        raise SystemExit("Give -o OUTPUT.mp4 (or --dump-captions FILE.json)")
    lang = None if (a.lang in (None, "", "auto")) else a.lang
    tmp = tempfile.mkdtemp()

    # ---- caption events -------------------------------------------------
    if a.captions_json:
        with open(os.path.expanduser(a.captions_json), encoding="utf-8") as f:
            data = json.load(f)
        caps = data["captions"] if isinstance(data, dict) else data
        script = detect_script(" ".join(c["text"] for c in caps))
        max_chars, _ = limits(script, a.size)
        events = []
        for c in caps:
            events += events_from_caption(float(c["start"]), float(c["end"]),
                                          str(c["text"]), max_chars)
    else:
        words, detected = get_words(video, a.start, dur, a.model, tmp, lang, a.hinglish)
        script = detect_script(" ".join(w["word"] for w in words))
        max_chars, max_words = limits(script, a.size)
        chunks = chunk_words(words, max_words, max_chars)
        if a.dump_captions:
            caps = []
            for i, ch in enumerate(chunks):
                end = ch[-1]["end"] + 0.15
                if i + 1 < len(chunks):
                    end = min(end, chunks[i + 1][0]["start"] - 0.01)
                caps.append({"start": round(ch[0]["start"], 2), "end": round(end, 2),
                             "text": " ".join(w["word"] for w in ch)})
            payload = {"source_lang": detected, "script": script,
                       "note": "Rewrite only the text fields. Keep start/end and the number of items.",
                       "captions": caps}
            path = os.path.abspath(os.path.expanduser(a.dump_captions))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            print(f"Wrote {len(caps)} captions ({detected}, {script}) -> {path}")
            return
        events = events_from_chunks(chunks)
    events = fix_overlaps(events, dur)

    # ---- layout inputs --------------------------------------------------
    out = os.path.abspath(os.path.expanduser(a.out))
    os.makedirs(os.path.dirname(out), exist_ok=True)
    cam_json = a.webcam or os.path.splitext(video)[0] + ".webcam.json"
    if not os.path.exists(cam_json):
        raise SystemExit(f"No webcam box found at {cam_json}. Run select_webcam.py first.")
    with open(cam_json) as f:
        cam = json.load(f)

    cap_font = pick_font(a.font, script)
    upper = script in ("latin", "cyrillic")
    ass = build_ass(events, dur, cap_font, a.ui_font, a.size, a.caption_y,
                    a.name, a.title, a.watermark, upper)
    with open(os.path.join(tmp, "captions.ass"), "w", encoding="utf-8") as f:
        f.write(ass)

    game_crop, game_box = "", None
    if a.game:
        game_box = [int(v) for v in a.game.split(",")]
    else:
        gjson = os.path.splitext(video)[0] + ".game.json"
        if os.path.exists(gjson):
            with open(gjson) as f:
                g = json.load(f)
            game_box = [g["x"], g["y"], g["w"], g["h"]]
    if game_box:
        gx, gy, gw, gh = game_box
        game_crop = f"crop={gw}:{gh}:{gx}:{gy},"

    sw, sh = cam.get("source_w", 10 ** 6), cam.get("source_h", 10 ** 6)
    pad = 8
    cx0, cy0 = max(0, cam["x"] - pad), max(0, cam["y"] - pad)
    cw0 = min(sw - cx0, cam["w"] + 2 * pad) // 2 * 2
    ch0 = min(sh - cy0, cam["h"] + 2 * pad) // 2 * 2
    if a.cover_cam == "blur":
        cover = (f"[g0]split=2[ga][gb];"
                 f"[gb]crop={cw0}:{ch0}:{cx0}:{cy0},gblur=sigma=40[gbb];"
                 f"[ga][gbb]overlay={cx0}:{cy0}[g1];")
    elif a.cover_cam == "black":
        cover = f"[g0]drawbox=x={cx0}:y={cy0}:w={cw0}:h={ch0}:color=black:t=fill[g1];"
    else:
        cover = "[g0]null[g1];"

    if has_filter("gradients"):
        band = (f"gradients=s={W}x{BAND_H}:c0=0xFF2E93:c1=0xFF9A3C:x0=0:y0=0:x1={W}:y1=0:"
                f"r=30:d={dur + 1:.2f}")
    else:
        band = f"color=c=0xFF5A64:s={W}x{BAND_H}:r=30:d={dur + 1:.2f}"

    fc = (
        f"[0:v]split=2[g0][c];"
        f"{cover}"
        f"[g1]{game_crop}fps=30,scale={W}:{TOP_H}:force_original_aspect_ratio=increase,"
        f"crop={W}:{TOP_H},setsar=1,format=yuv420p[top];"
        f"[c]crop={cam['w']}:{cam['h']}:{cam['x']}:{cam['y']},fps=30,"
        f"scale={W}:{CAM_H}:force_original_aspect_ratio=increase,"
        f"crop={W}:{CAM_H},setsar=1,format=yuv420p[cam];"
        f"{band},setsar=1,format=yuv420p[band];"
        f"[top][band][cam]vstack=inputs=3:shortest=1,ass=captions.ass[v]"
    )
    cmd = ["ffmpeg", "-v", "error", "-stats", "-y",
           "-ss", f"{a.start:.3f}", "-t", f"{dur:.3f}", "-i", video,
           "-filter_complex", fc, "-map", "[v]", "-map", "0:a:0?",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart",
           "-t", f"{dur:.3f}", out]
    subprocess.run(cmd, cwd=tmp, check=True)
    print(f"Saved {out}")


if __name__ == "__main__":
    main()

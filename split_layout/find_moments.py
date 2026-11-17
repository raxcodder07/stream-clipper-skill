#!/usr/bin/env python3
"""Transcribe a video and shortlist the most interesting ~30 s moments.

Usage (run with the venv that has faster-whisper):
  ~/.venvs/clip/bin/python find_moments.py VIDEO [--clips 8] [--len 30] [--model base]
                                           [--lang hi] [--keywords "bhai,kya baat"]

--lang     spoken language (default: auto-detect). For Hindi/other languages use
           --model small, the tiny/base models are weak outside English.
--keywords extra comma-separated words that mark exciting moments (any language).

Writes next to the video:
  VIDEO.words.json      every word with start/end seconds (reused by split_clip.py)
  VIDEO.transcript.txt  readable transcript with [HH:MM:SS] stamps
  VIDEO.moments.json    the ranked shortlist

Score = louder audio + faster speech + excited keywords. It only SHORTLISTS;
Claude should read the excerpts and make the final pick.
"""
import argparse
import bisect
import json
import os
import subprocess
import tempfile

import numpy as np

KEYWORDS = [
    "oh my god", "omg", "no way", "what the", "insane", "crazy", "holy", "wow",
    "unreal", "let's go", "lets go", "finally", "are you kidding", "shut up",
    "i can't", "cannot believe", "can't believe", "bro", "wait", "dead",
    "scream", "gta", "rockstar", "trailer", "pre-load", "preload", "download",
    "it's changed", "it's real", "mind", "disgusting", "beautiful",
]


def hms(t):
    t = int(t)
    return f"{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}"


def duration(video):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", video], capture_output=True, text=True, check=True).stdout
    return float(out.strip())


def loudness_per_second(video):
    cmd = ["ffmpeg", "-v", "error", "-i", video, "-vn", "-ac", "1", "-ar", "8000",
           "-f", "s16le", "-"]
    raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    a = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    n = len(a) // 8000
    if n == 0:
        return np.zeros(1)
    a = a[: n * 8000].reshape(n, 8000)
    return np.sqrt((a ** 2).mean(axis=1))


def z(x):
    x = np.asarray(x, dtype=np.float32)
    return (x - x.mean()) / (x.std() + 1e-6)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("video")
    ap.add_argument("--clips", type=int, default=8)
    ap.add_argument("--len", type=float, default=30.0, help="clip length in seconds (max 30)")
    ap.add_argument("--model", default="base", help="tiny, base, small ... (tiny is fastest)")
    ap.add_argument("--step", type=int, default=5)
    ap.add_argument("--lang", default=None, help="spoken language code, e.g. hi, es, ar")
    ap.add_argument("--keywords", default="", help="extra comma-separated hype words")
    a = ap.parse_args()
    lang = None if a.lang in (None, "", "auto") else a.lang
    keywords = KEYWORDS + [k.strip().lower() for k in a.keywords.split(",") if k.strip()]

    video = os.path.abspath(os.path.expanduser(a.video))
    base = os.path.splitext(video)[0]
    clip_len = min(a.len, 30.0)
    dur = duration(video)

    from faster_whisper import WhisperModel

    wav = os.path.join(tempfile.mkdtemp(), "audio.wav")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", video, "-vn", "-ac", "1",
                    "-ar", "16000", wav], check=True)
    print(f"Transcribing with model '{a.model}' (this can take a while)...")
    model = WhisperModel(a.model, device="cpu", compute_type="int8")
    segments, info = model.transcribe(wav, word_timestamps=True, vad_filter=True,
                                      beam_size=1, language=lang)
    detected = getattr(info, "language", None) or lang or ""

    words, segs = [], []
    for seg in segments:
        segs.append({"start": seg.start, "end": seg.end, "text": seg.text.strip()})
        for w in seg.words or []:
            if w.word.strip():
                words.append({"start": round(w.start, 3), "end": round(w.end, 3),
                              "word": w.word.strip()})

    with open(base + ".words.json", "w", encoding="utf-8") as f:
        json.dump(words, f, ensure_ascii=False)
    with open(base + ".words.lang", "w") as f:
        f.write(detected)
    with open(base + ".transcript.txt", "w", encoding="utf-8") as f:
        for s in segs:
            f.write(f"[{hms(s['start'])}] {s['text']}\n")

    loud = loudness_per_second(video)
    starts = [w["start"] for w in words]
    seg_starts = [s["start"] for s in segs]

    times, L, D, K, texts = [], [], [], [], []
    t = 0
    while t + clip_len <= max(dur, clip_len):
        i, j = bisect.bisect_left(starts, t), bisect.bisect_left(starts, t + clip_len)
        text = " ".join(w["word"] for w in words[i:j])
        low = text.lower()
        hits = sum(low.count(k) for k in keywords) + low.count("!")
        seg_loud = loud[int(t): int(t + clip_len)]
        times.append(t)
        L.append(float(seg_loud.mean()) if len(seg_loud) else 0.0)
        D.append((j - i) / clip_len)
        K.append(min(hits, 6))
        texts.append(text)
        t += a.step
    if not times:
        raise SystemExit("Video is too short.")

    score = z(L) + 0.7 * z(D) + 0.35 * np.asarray(K, dtype=np.float32)
    order = np.argsort(-score)
    chosen = []
    for idx in order:
        if all(abs(times[idx] - times[c]) >= clip_len for c in chosen):
            chosen.append(int(idx))
        if len(chosen) >= a.clips:
            break

    moments = []
    for rank, idx in enumerate(chosen, 1):
        t = times[idx]
        near = [s for s in seg_starts if t - 5 <= s <= t + 3]
        start = min(near, key=lambda s: abs(s - t)) if near else t
        end = min(start + clip_len, dur)
        moments.append({"rank": rank, "start": round(start, 2), "end": round(end, 2),
                        "score": round(float(score[idx]), 2), "text": texts[idx]})

    with open(base + ".moments.json", "w", encoding="utf-8") as f:
        json.dump(moments, f, indent=2, ensure_ascii=False)

    print(f"\nTop {len(moments)} candidate moments (saved to {base}.moments.json):\n")
    for m in moments:
        print(f"#{m['rank']}  {hms(m['start'])} - {hms(m['end'])}  score {m['score']}")
        print(f"    {m['text'][:220]}\n")


if __name__ == "__main__":
    main()

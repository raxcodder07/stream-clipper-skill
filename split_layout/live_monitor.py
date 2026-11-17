#!/usr/bin/env python3
"""Watch a YouTube live stream, spot hype moments, and cut clips automatically.

How it works
  1. Records the stream into a rolling buffer of short segments (stream copy, light on CPU).
  2. Measures loudness every second. A moment is flagged when the audio stays much louder
     than the recent average (a scream, a laugh, a big reaction).
  3. Optionally transcribes each segment and flags your keywords (--transcribe --keywords).
  4. When a moment is flagged it waits --after seconds, then saves the last --len seconds
     as a source clip in OUT/ and writes OUT/live-N.clip.json with start/end/reason.
  5. With --auto-render it also builds the finished split-screen Short.

Usage (run with the venv python):
  live_monitor.py URL [--out ~/clips/live] [--len 30] [--after 8] [--max-minutes 180]
  live_monitor.py URL --snapshot ~/clips/live/frame.mp4     # 5 s sample to pick the webcam box
  live_monitor.py URL --auto-render --webcam FRAME.webcam.json --name NOUGHT --title "LIVE REACTION"
  live_monitor.py FILE.mp4 --baseline 60 --warmup 30          # test on a local file

Only monitor streams you have permission to clip. Stop with Ctrl+C (or kill the process).
"""
import argparse
import collections
import glob
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time

import numpy as np

SR = 16000


def log(msg):
    print(time.strftime("[%H:%M:%S] ") + msg, flush=True)


def resolve(url, max_height, fmt_selector=None):
    """Return direct media URLs for a live stream using yt-dlp."""
    fmt = fmt_selector if fmt_selector else f"best[height<={max_height}]/best"
    r = subprocess.run(["yt-dlp", "-g", "-f", fmt, "--no-playlist", url],
                       capture_output=True, text=True)
    urls = [u for u in r.stdout.split() if u.startswith("http")]
    if not urls:
        raise RuntimeError("yt-dlp could not get the stream URL: "
                           + (r.stderr.strip().splitlines() or ["unknown error"])[-1])
    return urls


def start_recorder(inputs, buf, seg, realtime):
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    if realtime:
        cmd += ["-re"]
    for i in inputs:
        cmd += ["-i", i]
    if len(inputs) == 2:
        cmd += ["-map", "0:v:0", "-map", "1:a:0"]
    else:
        cmd += ["-map", "0:v:0", "-map", "0:a:0?"]
    cmd += ["-c", "copy", "-f", "segment", "-segment_time", str(seg),
            "-segment_format", "mpegts", "-reset_timestamps", "1",
            os.path.join(buf, "seg_%06d.ts")]
    return subprocess.Popen(cmd)


def audio_of(path):
    """Mono 16 kHz float audio of a segment."""
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-vn", "-ac", "1",
                          "-ar", str(SR), "-f", "f32le", "-"],
                         capture_output=True).stdout
    return np.frombuffer(raw, dtype=np.float32)


def per_second_rms(a):
    """Loudness per second. Segment lengths are rarely whole seconds, so split the audio
    into round(length) equal windows to keep the timeline true to the stream clock."""
    if len(a) < SR // 4:
        return []
    n = max(1, int(round(len(a) / SR)))
    return [float(np.sqrt((w ** 2).mean())) for w in np.array_split(a, n)]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", help="YouTube live URL (or a local video file for testing)")
    ap.add_argument("--out", default="~/clips/live")
    ap.add_argument("--len", type=float, default=30.0, help="clip length, max 30")
    ap.add_argument("--after", type=float, default=8.0, help="seconds to keep after the peak")
    ap.add_argument("--z", type=float, default=2.2, help="loudness threshold (std devs above normal)")
    ap.add_argument("--hold", type=int, default=3, help="seconds the loudness must persist")
    ap.add_argument("--baseline", type=int, default=300, help="seconds of history for 'normal'")
    ap.add_argument("--warmup", type=int, default=60, help="seconds to listen before flagging")
    ap.add_argument("--cooldown", type=int, default=90, help="min seconds between clips")
    ap.add_argument("--seg", type=int, default=10, help="buffer segment length in seconds")
    ap.add_argument("--keep-minutes", type=int, default=15, help="how long to keep buffer")
    ap.add_argument("--max-minutes", type=float, default=180, help="stop after this long")
    ap.add_argument("--max-height", type=int, default=720)
    ap.add_argument("--format", default=None, help="exact yt-dlp format selector (e.g. '312+234')")
    ap.add_argument("--transcribe", action="store_true", help="also flag keywords in speech")
    ap.add_argument("--keywords", default="", help="comma-separated hype words")
    ap.add_argument("--lang", default=None, help="spoken language code for --transcribe")
    ap.add_argument("--model", default="tiny")
    ap.add_argument("--realtime", action="store_true",
                    help="with a local file: play it at normal speed like a live stream")
    ap.add_argument("--snapshot", metavar="FILE.mp4",
                    help="save a 5 second sample and exit (to pick the webcam box)")
    ap.add_argument("--auto-render", action="store_true", help="also render the split-screen Short")
    ap.add_argument("--webcam", help="webcam JSON for --auto-render")
    ap.add_argument("--game", help="gameplay box x,y,w,h for --auto-render")
    ap.add_argument("--name", default="")
    ap.add_argument("--title", default="")
    ap.add_argument("--render-args", default="", help="extra options for split_clip.py")
    a = ap.parse_args()

    out = os.path.abspath(os.path.expanduser(a.out))
    os.makedirs(out, exist_ok=True)
    is_file = os.path.exists(os.path.expanduser(a.source))
    clip_len = min(a.len, 30.0)
    here = os.path.dirname(os.path.abspath(__file__))

    def get_inputs():
        return [os.path.abspath(os.path.expanduser(a.source))] if is_file \
            else resolve(a.source, a.max_height, a.format)

    if a.snapshot:
        snap = os.path.abspath(os.path.expanduser(a.snapshot))
        os.makedirs(os.path.dirname(snap), exist_ok=True)
        inputs = get_inputs()
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
        for i in inputs:
            cmd += ["-i", i]
        cmd += (["-map", "0:v:0", "-map", "1:a:0"] if len(inputs) == 2
                else ["-map", "0:v:0", "-map", "0:a:0?"])
        cmd += ["-t", "5", "-c", "copy", snap]
        subprocess.run(cmd, check=True)
        log(f"Saved sample {snap}. Now run: python3 select_webcam.py \"{snap}\"")
        return

    keywords = [k.strip().lower() for k in a.keywords.split(",") if k.strip()]
    model = None
    if a.transcribe:
        from faster_whisper import WhisperModel
        model = WhisperModel(a.model, device="cpu", compute_type="int8")

    buf = tempfile.mkdtemp(prefix="livebuf_", dir=out)
    log(f"Buffer: {buf}")
    log("Starting recorder... (Ctrl+C to stop)")
    proc = start_recorder(get_inputs(), buf, a.seg, a.realtime and is_file)
    failures = 0
    t_begin = time.time()

    seg_info = {}                       # idx -> {"start", "dur", "path"}
    loud = []                           # loudness per second since monitoring began
    zhist = collections.deque(maxlen=max(a.hold, 1))
    processed = -1
    pending = None                      # {"trigger": sec, "end": sec, "reason": str}
    cooldown_until = -1
    clip_n = len(glob.glob(os.path.join(out, "live-*.clip.json")))
    stream_id = time.strftime("%Y%m%d-%H%M%S")
    finished = False

    def cut(p):
        nonlocal clip_n
        end_t = min(p["end"], len(loud))
        want_start = max(0.0, end_t - clip_len)
        segs = [seg_info[i] for i in sorted(seg_info)
                if seg_info[i]["start"] + seg_info[i]["dur"] > want_start
                and seg_info[i]["start"] < end_t]
        if not segs:
            log("No buffered segments cover that moment (buffer too short), skipping.")
            return
        clip_n += 1
        name = f"live-{stream_id}-{clip_n:03d}"
        lst = os.path.join(buf, f"{name}.txt")
        with open(lst, "w") as f:
            for s in segs:
                f.write(f"file '{s['path']}'\n")
        dst = os.path.join(out, name + ".mp4")
        r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0",
                            "-i", lst, "-c", "copy", dst])
        if r.returncode != 0:
            log("Could not save the clip (ffmpeg error).")
            return
        start_off = max(0.0, want_start - segs[0]["start"])
        end_off = start_off + min(clip_len, end_t - want_start)
        meta = {"file": dst, "start": round(start_off, 2), "end": round(end_off, 2),
                "reason": p["reason"], "stream_second": round(p["trigger"], 1)}
        with open(os.path.join(out, name + ".clip.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2, ensure_ascii=False)
        log(f"CLIP_SAVED {dst} start={meta['start']} end={meta['end']} reason={p['reason']}")
        if a.auto_render:
            rcmd = [sys.executable, os.path.join(here, "split_clip.py"), dst,
                    "--start", str(meta["start"]), "--end", str(meta["end"]),
                    "-o", os.path.join(out, "rendered", name + ".mp4")]
            if a.webcam:
                rcmd += ["--webcam", a.webcam]
            if a.game:
                rcmd += ["--game", a.game]
            if a.name:
                rcmd += ["--name", a.name]
            if a.title:
                rcmd += ["--title", a.title]
            if a.lang:
                rcmd += ["--lang", a.lang]
            rcmd += shlex.split(a.render_args)
            r = subprocess.run(rcmd)
            log("RENDERED " + os.path.join(out, "rendered", name + ".mp4")
                if r.returncode == 0 else "Auto-render failed (clip is still saved).")

    try:
        while True:
            if (time.time() - t_begin) / 60 > a.max_minutes:
                log("Reached --max-minutes, stopping.")
                break
            rec_done = proc.poll() is not None
            if rec_done and not finished:
                if is_file:
                    finished = True
                else:
                    failures += 1
                    if failures > 3:
                        log("Recorder stopped and the stream cannot be reopened. Stream ended?")
                        finished = True
                    else:
                        log(f"Recorder stopped, retrying ({failures}/3)...")
                        time.sleep(10)
                        try:
                            proc = start_recorder(get_inputs(), buf, a.seg, False)
                        except Exception as e:  # noqa: BLE001
                            log(f"Retry failed: {e}")

            files = sorted(glob.glob(os.path.join(buf, "seg_*.ts")))
            idxs = [int(os.path.basename(f)[4:10]) for f in files]
            top = max(idxs) if idxs else -1
            for f, i in zip(files, idxs):
                if i <= processed:
                    continue
                if i == top and not (rec_done or finished):
                    break                         # newest segment may still be written
                a_audio = audio_of(f)
                secs = per_second_rms(a_audio)
                dur = max(len(a_audio) / SR, 0.5)
                start_t = float(len(loud))
                seg_info[i] = {"start": start_t, "dur": dur, "path": f}
                processed = i
                for x in secs:
                    hist = loud[-a.baseline:]
                    z = 0.0
                    if len(hist) >= a.warmup:
                        z = (x - np.mean(hist)) / (np.std(hist) + 1e-4)
                        if x < 0.005:
                            z = 0.0
                    loud.append(x)
                    zhist.append(z)
                    now = len(loud)
                    if (pending is None and now > cooldown_until and len(zhist) == zhist.maxlen
                            and min(zhist) >= a.z * 0.6 and float(np.mean(zhist)) >= a.z):
                        pending = {"trigger": now, "end": now + a.after,
                                   "reason": f"loudness z={np.mean(zhist):.1f}"}
                        cooldown_until = now + a.cooldown
                        log(f"Hype detected at stream second {now} ({pending['reason']}), "
                            f"waiting {a.after:.0f}s more...")
                if model is not None and keywords and len(a_audio) > SR:
                    segs, _ = model.transcribe(a_audio, language=a.lang, vad_filter=True,
                                               beam_size=1)
                    text = " ".join(s.text for s in segs).lower()
                    hit = next((k for k in keywords if k in text), None)
                    now = len(loud)
                    if hit and pending is None and now > cooldown_until:
                        pending = {"trigger": now, "end": now + a.after,
                                   "reason": f"keyword '{hit}'"}
                        cooldown_until = now + a.cooldown
                        log(f"Keyword '{hit}' heard at stream second {now}")
                # prune old segments
                for j in [j for j, s in seg_info.items()
                          if s["start"] < len(loud) - a.keep_minutes * 60]:
                    try:
                        os.remove(seg_info[j]["path"])
                    except OSError:
                        pass
                    del seg_info[j]
                if pending and len(loud) >= pending["end"]:
                    cut(pending)
                    pending = None
            if pending and finished:
                cut(pending)
                pending = None
            if finished and processed >= top:
                log("Stream ended. Done.")
                break
            time.sleep(1.0 if not is_file else 0.2)
    except KeyboardInterrupt:
        log("Stopped by user.")
    finally:
        if proc.poll() is None:
            proc.terminate()
        log(f"Finished. {clip_n} clip(s) in {out}")


if __name__ == "__main__":
    main()

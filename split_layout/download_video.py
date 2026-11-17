#!/usr/bin/env python3
"""Download YouTube videos (or just a part) into ~/clips/src/ for clipping.

Usage:
  download_video.py URL [URL ...] [--out ~/clips/src] [--max-height 1080]
                    [--sections 00:10:00-00:25:00] [--playlist] [--subs LANGS]
                    [--cookies-from-browser firefox] [--dry-run]

Examples:
  download_video.py https://youtu.be/VIDEOID
  download_video.py https://youtu.be/VIDEOID --sections 00:42:10-00:55:00
  download_video.py https://youtube.com/playlist?list=PLID --playlist
  download_video.py https://youtu.be/VIDEOID --subs hi,en

Files are named VIDEOID.mp4 (no spaces, easy to use in commands). The title is
saved next to it in VIDEOID.title.txt. Only download videos you have permission
to use. For a stream that is live right now use live_monitor.py instead.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("urls", nargs="+")
    ap.add_argument("--out", default="~/clips/src")
    ap.add_argument("--max-height", type=int, default=1080)
    ap.add_argument("--sections", help="download only START-END, e.g. 00:10:00-00:25:00")
    ap.add_argument("--playlist", action="store_true", help="download whole playlists")
    ap.add_argument("--subs", help="also download subtitles, comma-separated codes (e.g. en,hi)")
    ap.add_argument("--cookies-from-browser", metavar="BROWSER",
                    help="for age-restricted or members-only videos you can watch (firefox, chrome)")
    ap.add_argument("--dry-run", action="store_true", help="print the command, do not run it")
    a = ap.parse_args()

    out = os.path.abspath(os.path.expanduser(a.out))
    exe = shutil.which("yt-dlp")
    if not exe and not a.dry_run:
        sys.exit("yt-dlp not found. Install it with: pipx install yt-dlp")

    cmd = [exe or "yt-dlp",
           "-f", "bv*+ba/b", "-S", f"res:{a.max_height},ext:mp4:m4a",
           "--merge-output-format", "mp4",
           "-o", os.path.join(out, "%(id)s.%(ext)s"),
           "--print", "after_move:%(id)s\t%(title)s\t%(filepath)s",
           "--no-warnings", "--progress", "--newline"]
    cmd += ["--yes-playlist"] if a.playlist else ["--no-playlist"]
    if a.sections:
        cmd += ["--download-sections", f"*{a.sections}", "--force-keyframes-at-cuts"]
    if a.subs:
        cmd += ["--write-subs", "--write-auto-subs", "--sub-langs", a.subs,
                "--convert-subs", "srt"]
    if a.cookies_from_browser:
        cmd += ["--cookies-from-browser", a.cookies_from_browser]
    cmd += a.urls

    if a.dry_run:
        print(" ".join(f"'{c}'" if " " in c else c for c in cmd))
        return
    os.makedirs(out, exist_ok=True)
    r = subprocess.run(cmd, stdout=subprocess.PIPE, text=True)
    saved = []
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and os.path.exists(parts[2]):
            vid, title, path = parts
            with open(os.path.join(out, f"{vid}.title.txt"), "w", encoding="utf-8") as f:
                f.write(title + "\n")
            saved.append({"id": vid, "title": title, "path": path})
    if saved:
        print(json.dumps(saved, indent=2, ensure_ascii=False))
        print("\nReady for clipping: " + ", ".join(s["path"] for s in saved))
    if r.returncode != 0 or not saved:
        print("\nDownload failed. Common fixes: update yt-dlp (pipx upgrade yt-dlp); if the error "
              "mentions a JavaScript runtime, install one (e.g. deno); for age-restricted videos "
              "add --cookies-from-browser firefox.", file=sys.stderr)
        sys.exit(r.returncode or 1)


if __name__ == "__main__":
    main()

"""Automated clipping pipeline: source video -> best moments -> 9:16 captioned clips -> YouTube Shorts.

Runs unattended (see .github/workflows/clip.yml). State lives in state.json.
"""
import datetime as dt
import json
import logging
import os
import pathlib
import re
import subprocess
import sys

import yaml

ROOT = pathlib.Path(__file__).parent
WORK = ROOT / "work"
STATE_FILE = ROOT / "state.json"
CFG = yaml.safe_load((ROOT / "config.yaml").read_text())

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("clipper")

HOOK_WORDS = {
    "secret", "never", "always", "mistake", "truth", "actually", "crazy", "insane",
    "biggest", "worst", "best", "why", "how", "lesson", "problem", "money", "story",
    "learned", "surprised", "important", "warning", "stop", "different", "changed",
}


# ---------------------------------------------------------------- state
def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"done": [], "uploads": {}}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------- source
def ydl_opts(extra=None):
    opts = {"quiet": True, "no_warnings": True}
    if pathlib.Path(ROOT / "cookies.txt").exists():
        opts["cookiefile"] = str(ROOT / "cookies.txt")
    opts.update(extra or {})
    return opts


def list_new_videos(source_url, limit):
    from yt_dlp import YoutubeDL

    with YoutubeDL(ydl_opts({"extract_flat": True, "playlistend": limit})) as ydl:
        info = ydl.extract_info(source_url, download=False)
    return [e for e in (info.get("entries") or []) if e and e.get("id")]


def video_info(video_id):
    """Full metadata for one video (licence, channel name, duration)."""
    from yt_dlp import YoutubeDL

    with YoutubeDL(ydl_opts({"skip_download": True})) as ydl:
        return ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)


def download(video_id):
    from yt_dlp import YoutubeDL

    WORK.mkdir(exist_ok=True)
    out = WORK / f"{video_id}.mp4"
    if out.exists():
        return out
    fmt = "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080][ext=mp4]/b"
    opts = ydl_opts({"format": fmt, "outtmpl": str(WORK / f"{video_id}.%(ext)s"),
                     "merge_output_format": "mp4"})
    with YoutubeDL(opts) as ydl:
        ydl.download([f"https://www.youtube.com/watch?v={video_id}"])
    return out


# ---------------------------------------------------------------- transcript
def transcribe(path):
    from faster_whisper import WhisperModel

    model = WhisperModel(CFG["whisper_model"], device="cpu", compute_type="int8")
    segs, _ = model.transcribe(str(path), word_timestamps=True, vad_filter=True)
    out = []
    for s in segs:
        out.append({
            "start": s.start, "end": s.end, "text": s.text.strip(),
            "words": [{"w": w.word.strip(), "s": w.start, "e": w.end} for w in (s.words or [])],
        })
    return out


# ---------------------------------------------------------------- moment picking
def heuristic_clips(segs):
    lo, hi, n = CFG["min_clip_seconds"], CFG["max_clip_seconds"], CFG["clips_per_video"]
    cands = []
    for i, first in enumerate(segs):
        text, end = [], first["end"]
        for seg in segs[i:]:
            if seg["end"] - first["start"] > hi:
                break
            text.append(seg["text"])
            end = seg["end"]
            if end - first["start"] >= lo:
                joined = " ".join(text)
                words = re.findall(r"[a-z']+", joined.lower())
                dur = end - first["start"]
                score = (
                    2 * sum(w in HOOK_WORDS for w in words)
                    + joined.count("?") * 1.5
                    + joined.count("!") * 1.0
                    + (len(words) / dur) * 2  # speech density
                )
                cands.append({"start": first["start"], "end": end, "score": score, "text": joined})
    cands.sort(key=lambda c: c["score"], reverse=True)
    picked = []
    for c in cands:
        if all(c["end"] <= p["start"] or c["start"] >= p["end"] for p in picked):
            picked.append(c)
        if len(picked) >= n:
            break
    for c in picked:
        c["title"] = make_title(c["text"])
    return picked


def make_title(text):
    first = re.split(r"(?<=[.!?])\s", text.strip())[0]
    first = first[:70].rsplit(" ", 1)[0] if len(first) > 70 else first
    return first.strip(" .,") or "Best moment"


def llm_clips(segs, video_title):
    """Optional: let Claude choose moments. Needs ANTHROPIC_API_KEY. Returns None on any failure."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key or not CFG.get("use_claude", True):
        return None
    try:
        import anthropic

        transcript = "\n".join(f"[{s['start']:.1f}-{s['end']:.1f}] {s['text']}" for s in segs)[:60000]
        prompt = (
            f"Video title: {video_title}\n\nTranscript with timestamps:\n{transcript}\n\n"
            f"Pick the {CFG['clips_per_video']} most engaging, self-contained moments for short-form "
            f"vertical video. Each must be {CFG['min_clip_seconds']}-{CFG['max_clip_seconds']} seconds, "
            "start on a strong hook, end on a complete thought, and not overlap. "
            'Reply with ONLY JSON: [{"start": float, "end": float, "title": "max 70 chars"}]'
        )
        client = anthropic.Anthropic(api_key=key)
        msg = client.messages.create(model=CFG["claude_model"], max_tokens=1000,
                                     messages=[{"role": "user", "content": prompt}])
        raw = msg.content[0].text
        clips = json.loads(raw[raw.index("["): raw.rindex("]") + 1])
        ok = []
        for c in clips:
            d = float(c["end"]) - float(c["start"])
            if CFG["min_clip_seconds"] - 5 <= d <= CFG["max_clip_seconds"] + 5:
                ok.append({"start": float(c["start"]), "end": float(c["end"]),
                           "title": str(c["title"])[:90], "text": ""})
        return ok or None
    except Exception as e:  # noqa: BLE001
        log.warning("Claude pick failed, using heuristic: %s", e)
        return None


# ---------------------------------------------------------------- render
def ass_time(t):
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def write_ass(segs, start, end, path):
    words = [w for s in segs for w in s["words"] if w["s"] >= start and w["e"] <= end]
    header = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\n\n"
        "[V4+ Styles]\nFormat: Name,Fontname,Fontsize,PrimaryColour,OutlineColour,BackColour,Bold,"
        "Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,"
        "Alignment,MarginL,MarginR,MarginV,Encoding\n"
        "Style: Default,DejaVu Sans,78,&H00FFFFFF,&H00000000,&H00000000,1,0,0,0,100,100,0,0,1,6,2,2,60,60,520,1\n\n"
        "[Events]\nFormat: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text\n"
    )
    lines = []
    for i in range(0, len(words), 3):
        grp = words[i:i + 3]
        txt = " ".join(w["w"] for w in grp).upper().replace("{", "").replace("}", "")
        lines.append(f"Dialogue: 0,{ass_time(grp[0]['s'] - start)},{ass_time(grp[-1]['e'] - start)},Default,,0,0,0,,{txt}")
    path.write_text(header + "\n".join(lines) + "\n")


def render(src, segs, clip, name):
    ass = WORK / f"{name}.ass"
    out = WORK / f"{name}_short.mp4"
    write_ass(segs, clip["start"], clip["end"], ass)
    vf = (
        "[0:v]split[a][b];"
        "[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=25[bg];"
        "[b]scale=1080:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2,"
        f"ass={ass.name}[v]"
    )
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error", "-ss", f"{clip['start']:.2f}", "-to", f"{clip['end']:.2f}",
        "-i", str(src.resolve()), "-filter_complex", vf, "-map", "[v]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart", str(out.name),
    ]
    subprocess.run(cmd, check=True, cwd=WORK)
    return out


# ---------------------------------------------------------------- upload
def upload_youtube(path, title, description):
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    creds = Credentials(
        None,
        refresh_token=os.environ["YT_REFRESH_TOKEN"],
        client_id=os.environ["YT_CLIENT_ID"],
        client_secret=os.environ["YT_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
    )
    yt = build("youtube", "v3", credentials=creds)
    body = {
        "snippet": {
            "title": f"{title[:88]} #Shorts",
            "description": description,
            "tags": CFG["tags"],
            "categoryId": str(CFG["category_id"]),
        },
        "status": {"privacyStatus": CFG["privacy"], "selfDeclaredMadeForKids": False},
    }
    req = yt.videos().insert(part="snippet,status", body=body,
                             media_body=MediaFileUpload(str(path), chunksize=-1, resumable=True))
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    return resp["id"]


# ---------------------------------------------------------------- main
def main():
    if not CFG.get("rights_confirmed"):
        sys.exit("Set rights_confirmed: true in config.yaml only if you own or are licensed to clip every source.")

    state = load_state()
    today = dt.date.today().isoformat()
    uploaded_today = state["uploads"].get(today, 0)
    dry = os.environ.get("DRY_RUN") == "1"
    processed = 0

    for src_url in CFG["sources"]:
        for entry in list_new_videos(src_url, CFG["scan_latest"]):
            vid = entry["id"]
            if vid in state["done"]:
                continue
            if processed >= CFG["max_videos_per_run"] or uploaded_today >= CFG["max_uploads_per_day"]:
                break
            title = entry.get("title") or vid
            channel = entry.get("channel") or entry.get("uploader") or "the original creator"
            if CFG.get("require_cc_license"):
                try:
                    info = video_info(vid)
                except Exception as e:  # noqa: BLE001
                    log.warning("Could not read info for %s: %s", vid, e)
                    continue
                lic = (info.get("license") or "").lower()
                duration = info.get("duration") or 0
                if "creative commons" not in lic or duration < CFG.get("min_source_seconds", 180):
                    state["done"].append(vid)  # not CC or too short: never check again
                    save_state(state)
                    continue
                channel = info.get("channel") or info.get("uploader") or channel
                title = info.get("title") or title
            log.info("Processing %s (%s)", vid, title)
            try:
                src = download(vid)
                segs = transcribe(src)
                if not segs:
                    raise RuntimeError("no speech found")
                clips = llm_clips(segs, title) or heuristic_clips(segs)
                for n, clip in enumerate(clips):
                    if uploaded_today >= CFG["max_uploads_per_day"]:
                        break
                    out = render(src, segs, clip, f"{vid}_{n}")
                    if dry:
                        log.info("DRY RUN: rendered %s (%s)", out, clip["title"])
                        continue
                    desc = CFG["description_template"].format(
                        title=title, channel=channel, url=f"https://youtu.be/{vid}")
                    new_id = upload_youtube(out, clip["title"], desc)
                    log.info("Uploaded https://youtube.com/shorts/%s", new_id)
                    uploaded_today += 1
                    state["uploads"][today] = uploaded_today
                state["done"].append(vid)
            except Exception as e:  # noqa: BLE001
                log.error("Failed on %s: %s", vid, e)
                state["done"].append(vid)  # don't retry forever; remove from state.json to retry
            finally:
                save_state(state)
                for f in WORK.glob(f"{vid}*"):
                    f.unlink(missing_ok=True)
            processed += 1
    log.info("Done. Videos processed: %d, uploads today: %d", processed, uploaded_today)


if __name__ == "__main__":
    main()

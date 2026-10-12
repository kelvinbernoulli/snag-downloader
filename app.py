import os
import shutil
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import yt_dlp
from flask import Flask, jsonify, request, send_file

app = Flask(__name__, static_folder="static", static_url_path="")
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024  # cookies files are small

MAX_PLAYLIST = 100          # cap per playlist
KEEP_SECONDS = 3600         # finished files are deleted after 1 hour
pool = ThreadPoolExecutor(max_workers=2)   # 2 downloads at once, the rest queue
JOBS = {}


@app.get("/")
def index():
    return app.send_static_file("index.html")


def explain(err):
    """Turn a yt-dlp error into a clear message and a needs_cookies flag."""
    m = str(err).lower()
    if "drm" in m:
        return ("This video uses DRM protection and can't be downloaded with this tool.", False)
    if any(k in m for k in ("sign in", "log in", "login", "cookies", "members-only",
                            "age-restricted", "age verification", "private video", "confirm you")):
        return ("This link needs you to be signed in. Add a cookies.txt exported from a browser "
                "where you're logged in with access, then try again.", True)
    return ("Couldn't read that link. It may be unsupported, unavailable, or region-locked.", False)


def save_cookies(folder):
    f = request.files.get("cookies")
    if f and f.filename:
        path = os.path.join(folder, "cookies.txt")
        f.save(path)
        return {"cookiefile": path}
    return {}


def valid_url(u):
    return u.startswith(("http://", "https://"))


@app.post("/api/info")
def info():
    url = request.form.get("url", "").strip()
    if not valid_url(url):
        return jsonify(error="Enter a full link starting with http:// or https://"), 400
    ck = tempfile.mkdtemp(prefix="ytck_")
    try:
        opts = {"quiet": True, "extract_flat": "in_playlist", **save_cookies(ck)}
        with yt_dlp.YoutubeDL(opts) as ydl:
            d = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        msg, needs = explain(e)
        return jsonify(error=msg, needs_cookies=needs), 400
    finally:
        shutil.rmtree(ck, ignore_errors=True)

    if d.get("_type") == "playlist":
        entries = [e for e in (d.get("entries") or []) if e]
        thumbs = entries[0].get("thumbnails") if entries else None
        return jsonify(is_playlist=True, title=d.get("title"),
                       uploader=d.get("uploader") or d.get("channel"),
                       count=min(len(entries), MAX_PLAYLIST), total=len(entries),
                       thumbnail=d.get("thumbnail") or (thumbs[-1].get("url") if thumbs else None),
                       heights=[2160, 1440, 1080, 720, 480, 360])

    heights = sorted({f["height"] for f in d.get("formats", []) if f.get("height")}, reverse=True)
    return jsonify(is_playlist=False, title=d.get("title"), uploader=d.get("uploader"),
                   duration=d.get("duration"), thumbnail=d.get("thumbnail"), heights=heights)


def run_job(j, url, kind, height, playlist, subs, cookie_opts):
    def hook(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            frac = (d.get("downloaded_bytes") or 0) / total if total else 0
            meta = d.get("info_dict") or {}
            n = meta.get("n_entries") or 1
            i = meta.get("playlist_index") or 1
            pct = min(((i - 1) + frac) / n * 100, 99)
            j["percent"] = max(j["percent"], round(pct, 1))   # never move backwards
            j["message"] = f"Downloading{f' ({i} of {n})' if n > 1 else ''}…"
        elif d["status"] == "finished":
            j["message"] = "Processing…"

    tmpl = "%(playlist_index)03d - %(title).100B.%(ext)s" if playlist else "%(title).120B.%(ext)s"
    opts = {
        "outtmpl": os.path.join(j["out"], tmpl),
        "noplaylist": not playlist, "playlistend": MAX_PLAYLIST,
        "ignoreerrors": bool(playlist),   # skip broken playlist entries instead of failing all
        "quiet": True, "restrictfilenames": True,
        "progress_hooks": [hook], **cookie_opts,
    }
    pps = []
    if kind == "audio":
        opts["format"] = "bestaudio/best"
        pps.append({"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"})
    else:
        opts["format"] = f"bv*[height<={height}]+ba/b[height<={height}]/b"
        opts["merge_output_format"] = "mp4"
        if subs:
            opts.update(writesubtitles=True, writeautomaticsub=True, subtitleslangs=["en.*"])
            pps.append({"key": "FFmpegSubtitlesConvertor", "format": "srt"})
    opts["postprocessors"] = pps

    try:
        j["status"] = "downloading"
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
        files = sorted(f for f in os.listdir(j["out"]) if os.path.isfile(os.path.join(j["out"], f)))
        if not files:
            raise RuntimeError("no files were produced")
        if len(files) == 1:
            j["file"], j["name"] = os.path.join(j["out"], files[0]), files[0]
        else:
            j["message"] = "Zipping…"
            base = j["out"] + "_bundle"
            j["file"] = shutil.make_archive(base, "zip", j["out"])
            j["name"] = "download.zip"
        j["percent"], j["status"], j["message"] = 100, "done", "Ready"
    except Exception as e:
        msg, needs = explain(e)
        if msg.startswith("Couldn't"):
            msg = "Download failed. Check the link and that ffmpeg is installed."
        j.update(status="error", error=msg, needs_cookies=needs)
    finally:
        shutil.rmtree(j["ck"], ignore_errors=True)   # cookies never outlive the download


def reap():
    now = time.time()
    for jid, j in list(JOBS.items()):
        if j["status"] in ("done", "error") and now - j["created"] > KEEP_SECONDS:
            shutil.rmtree(j["out"], ignore_errors=True)
            if j.get("file") and j["file"].endswith("_bundle.zip"):
                try:
                    os.remove(j["file"])
                except OSError:
                    pass
            JOBS.pop(jid, None)


@app.post("/api/jobs")
def create_job():
    url = request.form.get("url", "").strip()
    if not valid_url(url):
        return jsonify(error="Invalid link."), 400
    reap()
    kind = "audio" if request.form.get("kind") == "audio" else "video"
    try:
        height = int(request.form.get("height") or 2160)
    except ValueError:
        height = 2160
    playlist = request.form.get("playlist") == "1"
    subs = request.form.get("subs") == "1"

    ck = tempfile.mkdtemp(prefix="ytck_")
    cookie_opts = save_cookies(ck)
    jid = uuid.uuid4().hex
    j = {"status": "queued", "percent": 0, "message": "Waiting in queue…", "created": time.time(),
         "out": tempfile.mkdtemp(prefix="ytdl_"), "ck": ck, "file": None, "name": None}
    JOBS[jid] = j
    pool.submit(run_job, j, url, kind, height, playlist, subs, cookie_opts)
    return jsonify(id=jid)


@app.get("/api/jobs/<jid>")
def job_status(jid):
    j = JOBS.get(jid)
    if not j:
        return jsonify(error="This download expired. Add it to the queue again."), 404
    return jsonify({k: j.get(k) for k in ("status", "percent", "message", "name", "error", "needs_cookies")})


@app.get("/api/jobs/<jid>/file")
def job_file(jid):
    j = JOBS.get(jid)
    if not j or j["status"] != "done":
        return jsonify(error="File isn't ready."), 404
    return send_file(j["file"], as_attachment=True, download_name=j["name"])


if __name__ == "__main__":
    app.run(debug=True, port=5000)

import os
import uuid
import json
import time
import threading
import re

from flask import Flask, render_template, request, jsonify, send_file, Response
import yt_dlp

app = Flask(__name__)

DOWNLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

jobs = {}
jobs_lock = threading.Lock()


def safe_filename(title: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "", title).strip()[:100]


def download_worker(job_id: str, url: str, fmt: str):
    with jobs_lock:
        job = jobs[job_id]

    def progress_hook(d):
        if d["status"] == "downloading":
            raw = d.get("_percent_str", "0%").strip().replace("%", "")
            try:
                pct = float(raw)
            except ValueError:
                pct = 0.0
            with jobs_lock:
                job["progress"] = pct
                job["speed"] = d.get("_speed_str", "").strip()
                job["eta"] = d.get("_eta_str", "").strip()
                job["status"] = "downloading"
        elif d["status"] == "finished":
            with jobs_lock:
                job["progress"] = 99
                job["status"] = "processing"

    output_template = os.path.join(DOWNLOAD_DIR, f"{job_id}.%(ext)s")

    if fmt == "mp3":
        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": output_template,
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }
            ],
            "progress_hooks": [progress_hook],
            "quiet": True,
            "no_warnings": True,
        }
        out_ext = "mp3"
    else:
        ydl_opts = {
            "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "outtmpl": output_template,
            "merge_output_format": "mp4",
            "progress_hooks": [progress_hook],
            "quiet": True,
            "no_warnings": True,
        }
        out_ext = "mp4"

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            title = info.get("title", "download")
            with jobs_lock:
                job["title"] = title
                job["filename"] = f"{job_id}.{out_ext}"
                job["progress"] = 100
                job["status"] = "done"
    except Exception as exc:
        with jobs_lock:
            job["status"] = "error"
            job["error"] = str(exc)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/start", methods=["POST"])
def api_start():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    fmt = data.get("format", "mp4")

    if not url:
        return jsonify({"error": "No URL provided"}), 400
    if fmt not in ("mp4", "mp3"):
        fmt = "mp4"

    job_id = str(uuid.uuid4())
    with jobs_lock:
        jobs[job_id] = {
            "status": "downloading",
            "progress": 0,
            "speed": "",
            "eta": "",
            "filename": None,
            "title": None,
            "error": None,
        }

    t = threading.Thread(target=download_worker, args=(job_id, url, fmt), daemon=True)
    t.start()

    return jsonify({"job_id": job_id})


@app.route("/api/progress/<job_id>")
def api_progress(job_id):
    def stream():
        while True:
            with jobs_lock:
                if job_id not in jobs:
                    yield f"data: {json.dumps({'status': 'error', 'error': 'Job not found'})}\n\n"
                    return
                snapshot = dict(jobs[job_id])

            yield f"data: {json.dumps(snapshot)}\n\n"

            if snapshot["status"] in ("done", "error"):
                return

            time.sleep(0.4)

    return Response(
        stream(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/file/<job_id>")
def api_file(job_id):
    with jobs_lock:
        job = jobs.get(job_id)

    if not job:
        return jsonify({"error": "Job not found"}), 404
    if job["status"] != "done":
        return jsonify({"error": "File not ready"}), 400

    filepath = os.path.join(DOWNLOAD_DIR, job["filename"])
    if not os.path.exists(filepath):
        return jsonify({"error": "File missing on server"}), 404

    ext = job["filename"].rsplit(".", 1)[-1]
    download_name = f"{safe_filename(job['title'] or 'download')}.{ext}"

    def remove_after():
        time.sleep(60)
        try:
            os.remove(filepath)
        except OSError:
            pass
        with jobs_lock:
            jobs.pop(job_id, None)

    threading.Thread(target=remove_after, daemon=True).start()

    return send_file(filepath, as_attachment=True, download_name=download_name)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print("\n  Quick YouTube Downloader is running!")
    print(f"  Open http://localhost:{port} in your browser\n")
    app.run(host="0.0.0.0", port=port, debug=False)

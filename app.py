import os
import uuid
import json
import time
import threading
import re

from flask import Flask, render_template, request, jsonify, send_file, Response
import yt_dlp
import imageio_ffmpeg

FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()

app = Flask(__name__)

DOWNLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)


def safe_filename(title: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "", title).strip()[:100]


def state_path(job_id: str) -> str:
    return os.path.join(DOWNLOAD_DIR, f"{job_id}.json")


def read_job(job_id: str):
    try:
        with open(state_path(job_id)) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def write_job(job_id: str, state: dict):
    tmp = state_path(job_id) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, state_path(job_id))


def update_job(job_id: str, **kwargs):
    state = read_job(job_id) or {}
    state.update(kwargs)
    write_job(job_id, state)


def download_worker(job_id: str, url: str, fmt: str):
    def progress_hook(d):
        if d["status"] == "downloading":
            raw = d.get("_percent_str", "0%").strip().replace("%", "")
            try:
                pct = float(raw)
            except ValueError:
                pct = 0.0
            update_job(job_id,
                       status="downloading",
                       progress=pct,
                       speed=d.get("_speed_str", "").strip(),
                       eta=d.get("_eta_str", "").strip())
        elif d["status"] == "finished":
            update_job(job_id, status="processing", progress=99)

    output_template = os.path.join(DOWNLOAD_DIR, f"{job_id}.%(ext)s")

    common_opts = {
        "ffmpeg_location": FFMPEG_PATH,
        "concurrent_fragment_downloads": 8,
        "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
        "progress_hooks": [progress_hook],
        "quiet": True,
        "no_warnings": True,
    }

    ydl_opts = {
        **common_opts,
        "format": "bestaudio[ext=m4a]/bestaudio",
        "outtmpl": output_template,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "m4a",
            }
        ],
    }
    out_ext = "m4a"

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            title = info.get("title", "download")
            update_job(job_id,
                       status="done",
                       progress=100,
                       title=title,
                       filename=f"{job_id}.{out_ext}")
    except Exception as exc:
        update_job(job_id, status="error", error=str(exc))


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
    fmt = "m4a"

    job_id = str(uuid.uuid4())
    write_job(job_id, {
        "status": "downloading",
        "progress": 0,
        "speed": "",
        "eta": "",
        "filename": None,
        "title": None,
        "error": None,
    })

    t = threading.Thread(target=download_worker, args=(job_id, url, fmt), daemon=True)
    t.start()

    return jsonify({"job_id": job_id})


@app.route("/api/progress/<job_id>")
def api_progress(job_id):
    def stream():
        while True:
            job = read_job(job_id)
            if job is None:
                yield f"data: {json.dumps({'status': 'error', 'error': 'Job not found'})}\n\n"
                return

            yield f"data: {json.dumps(job)}\n\n"

            if job["status"] in ("done", "error"):
                return

            time.sleep(0.4)

    return Response(
        stream(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/file/<job_id>")
def api_file(job_id):
    job = read_job(job_id)

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
        try:
            os.remove(state_path(job_id))
        except OSError:
            pass

    threading.Thread(target=remove_after, daemon=True).start()

    return send_file(filepath, as_attachment=True, download_name=download_name)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print("\n  Quick YouTube Downloader is running!")
    print(f"  Open http://localhost:{port} in your browser\n")
    app.run(host="0.0.0.0", port=port, debug=False)

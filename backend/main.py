"""VideoForge API server.

Run:  .venv\\Scripts\\python -m uvicorn backend.main:app --port 8321
"""
import json
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, ingest, llm, repurpose, youtube_up

try:  # optional private module (not in the public repo)
    from . import generate_api
except ImportError:
    generate_api = None

app = FastAPI(title="VideoForge")


@app.middleware("http")
async def no_stale_cache(request, call_next):
    """Browsers must revalidate the app and media on every load — a cached old
    app.js silently hides new features (e.g. upload badges). Revalidation via
    ETag/Last-Modified is near-free on localhost."""
    resp = await call_next(request)
    p = request.url.path
    if p == "/" or p.startswith(("/static", "/media")):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


# ------------------------------------------------------------------ jobs

class Job:
    def __init__(self, kind: str, label: str = ""):
        self.id = uuid.uuid4().hex[:10]
        self.kind = kind
        self.label = label
        self.status = "queued"          # queued | running | done | error
        self.progress = 0.0
        self.message = ""
        self.logs: list[str] = []
        self.result = None
        self.error = ""
        self.created = time.time()
        self.started: float | None = None

    def set(self, progress: float, message: str = ""):
        self.progress = round(float(progress), 3)
        if message:
            self.message = message

    def log(self, line: str):
        self.logs.append(f"[{time.strftime('%H:%M:%S')}] {line}")

    def eta_seconds(self):
        """Rough remaining time from the observed progress rate."""
        if self.status != "running" or not self.started or self.progress < 0.08:
            return None
        elapsed = time.time() - self.started
        return round(elapsed * (1 - self.progress) / max(self.progress, 0.01))

    def to_dict(self):
        return {"id": self.id, "kind": self.kind, "label": self.label,
                "status": self.status, "progress": self.progress,
                "message": self.message, "eta_seconds": self.eta_seconds(),
                "logs": self.logs[-40:], "result": self.result, "error": self.error}


JOBS: dict[str, Job] = {}
# Generation jobs run strictly one at a time (batch queue): the laptop can't
# encode two renders at once anyway, and serial order makes ETAs meaningful.
GEN_POOL = ThreadPoolExecutor(max_workers=1)


def _spawn(job: Job, target, *args, serial: bool = False, **kwargs):
    def wrap():
        job.status = "running"
        job.started = time.time()
        try:
            target(job, *args, **kwargs)
            job.status = "done"
        except Exception as e:  # surface every failure to the UI
            job.status = "error"
            job.error = str(e)
            job.log(f"ERROR: {e}")
    JOBS[job.id] = job
    if serial:
        job.message = "waiting in queue"
        GEN_POOL.submit(wrap)
    else:
        threading.Thread(target=wrap, daemon=True).start()
    return {"job_id": job.id}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return job.to_dict()


# ------------------------------------------------------------------ status

@app.get("/api/status")
def status():
    total, used, free = shutil.disk_usage(str(config.ROOT))  # the drive media lives on
    return {
        "llm": llm.backend_status(),
        "youtube_configured": youtube_up.is_configured(),
        "youtube_quota": youtube_up.quota_status(),
        "disk_free_gb": round(free / 1e9, 2),
        "ollama_model": config.OLLAMA_MODEL,
        "features": {"generate": generate_api is not None},
    }


@app.get("/api/youtube/channels")
def youtube_channels():
    return {"channels": youtube_up.channels_status(),
            "quota": youtube_up.quota_status()}


# ------------------------------------------------------------------ workflow A

class RepurposeReq(BaseModel):
    url: str
    reaction_vo: bool = False       # spoken hype line mixed over each clip intro
    commentary_vo: bool = False     # full English narration over the whole clip
    skip_thumbnail: bool = True     # ban embedded thumbnail/title-card frames
    frame: str = "fit"              # "fit" (blurred bars) | "fill" (full-portrait crop)


def _rep_options(reaction_vo: bool, commentary_vo: bool,
                 skip_thumbnail: bool, frame: str) -> dict:
    return {"reaction_vo": reaction_vo, "commentary_vo": commentary_vo,
            "skip_thumbnail": skip_thumbnail,
            "frame": frame if frame in ("fit", "fill") else "fit"}


@app.post("/api/repurpose/url")
def repurpose_url(req: RepurposeReq):
    if not ingest.is_url(req.url):
        raise HTTPException(400, "not a valid http(s) URL")
    return _spawn(Job("repurpose"), repurpose.run, req.url,
                  _rep_options(req.reaction_vo, req.commentary_vo,
                               req.skip_thumbnail, req.frame))


@app.post("/api/repurpose/upload")
async def repurpose_upload(file: UploadFile = File(...),
                           reaction_vo: bool = Form(False),
                           commentary_vo: bool = Form(False),
                           skip_thumbnail: bool = Form(True),
                           frame: str = Form("fit")):
    data = await file.read()
    if len(data) > 1_500_000_000:
        raise HTTPException(413, "file too large")
    info = ingest.save_upload(file.filename or "upload.mp4", data)
    return _spawn(Job("repurpose"), repurpose.run, info,
                  _rep_options(reaction_vo, commentary_vo, skip_thumbnail, frame))


if generate_api:
    generate_api.register(app, _spawn, Job)


# ------------------------------------------------------------------ library

@app.get("/api/library")
def library():
    def collect(folder: Path, kind: str):
        items = []
        for f in sorted(folder.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True):
            meta = {}
            mf = f.with_suffix(".json")
            if mf.exists():
                try:
                    meta = json.loads(mf.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    pass
            items.append({"file": f.name, "kind": kind, "size_mb": round(f.stat().st_size / 1e6, 1),
                          "url": f"/media/{kind}/{f.name}", "meta": meta})
        return items
    return {"clips": collect(config.CLIPS_DIR, "clips"),
            "renders": collect(config.RENDERS_DIR, "renders")}


@app.get("/api/storage")
def storage():
    """Disk breakdown across the tool's folders + how much is reclaimable by
    deleting already-uploaded clips/renders and stale source videos."""
    def dir_bytes(p: Path) -> int:
        return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())

    uploaded_ct = uploaded_bytes = 0
    for folder, kind in ((config.CLIPS_DIR, "clips"), (config.RENDERS_DIR, "renders")):
        for mf in folder.glob("*.json"):
            try:
                meta = json.loads(mf.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            vid = mf.with_suffix(".mp4")
            if meta.get("youtube") and vid.exists():
                uploaded_ct += 1
                uploaded_bytes += vid.stat().st_size
    ingest_bytes = dir_bytes(config.INGEST_DIR)
    import shutil
    free = shutil.disk_usage(str(config.ROOT))[2]
    return {
        "folders": {
            "clips": round(dir_bytes(config.CLIPS_DIR) / 1e6, 1),
            "renders": round(dir_bytes(config.RENDERS_DIR) / 1e6, 1),
            "ingest": round(ingest_bytes / 1e6, 1),
            "frames": round(dir_bytes(config.FRAMES_DIR) / 1e6, 1),
            "models": round(dir_bytes(config.MODELS_DIR) / 1e6, 1),
        },
        "reclaimable": {
            "uploaded_items": uploaded_ct,
            "uploaded_mb": round(uploaded_bytes / 1e6, 1),
            "source_videos_mb": round(ingest_bytes / 1e6, 1),
        },
        "disk_free_gb": round(free / 1e9, 2),
    }


@app.post("/api/library/delete-uploaded")
def delete_uploaded():
    """Free space by deleting every clip/render already on YouTube (keeps its
    metadata sidecar as a lightweight record of the upload)."""
    deleted, freed = [], 0
    for folder, kind in ((config.CLIPS_DIR, "clips"), (config.RENDERS_DIR, "renders")):
        for mf in folder.glob("*.json"):
            try:
                meta = json.loads(mf.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            vid = mf.with_suffix(".mp4")
            if meta.get("youtube") and vid.exists():
                freed += vid.stat().st_size
                vid.unlink()
                # keep the .json (now video-less) so the upload stays on record
                meta["video_deleted"] = True
                mf.write_text(json.dumps(meta, indent=2), encoding="utf-8")
                deleted.append(f"{kind}/{vid.name}")
    return {"deleted": len(deleted), "freed_mb": round(freed / 1e6, 1), "files": deleted}


@app.post("/api/library/clear-sources")
def clear_sources():
    """Delete cached source downloads in ingest/ (they're only needed during a
    job; a re-download fetches them again)."""
    freed = 0
    for f in config.INGEST_DIR.iterdir():
        if f.is_file():
            freed += f.stat().st_size
            f.unlink(missing_ok=True)
    return {"freed_mb": round(freed / 1e6, 1)}


@app.delete("/api/library/{kind}/{name}")
def delete_item(kind: str, name: str):
    folder = {"clips": config.CLIPS_DIR, "renders": config.RENDERS_DIR}.get(kind)
    if not folder or "/" in name or "\\" in name or ".." in name:
        raise HTTPException(400, "bad path")
    f = folder / name
    if not f.exists():
        raise HTTPException(404, "not found")
    f.unlink()
    f.with_suffix(".json").unlink(missing_ok=True)
    return {"ok": True}


# ------------------------------------------------------------------ youtube

class YTReq(BaseModel):
    kind: str
    file: str
    title: str
    description: str = ""
    hashtags: list[str] = []
    privacy: str = "public"
    channel: str = config.DEFAULT_CHANNEL


ACTIVE_UPLOADS: set[tuple] = set()          # (kind, file, channel) currently in flight
_uploads_lock = threading.Lock()


@app.post("/api/youtube/upload")
def yt_upload(req: YTReq):
    folder = {"clips": config.CLIPS_DIR, "renders": config.RENDERS_DIR}.get(req.kind)
    if not folder or "/" in req.file or "\\" in req.file or ".." in req.file:
        raise HTTPException(400, "bad path")
    path = folder / req.file
    if not path.exists():
        raise HTTPException(404, "video not found")
    if req.privacy not in ("private", "unlisted", "public"):
        raise HTTPException(400, "privacy must be private/unlisted/public")
    if req.channel not in config.YT_CHANNELS:
        raise HTTPException(400, f"channel must be one of: {', '.join(config.YT_CHANNELS)}")
    if not youtube_up.is_configured():
        raise HTTPException(400, "YouTube not configured: put client_secret.json in the "
                                 "project root (see README setup steps)")
    try:  # refuse immediately if today's free quota is spent
        youtube_up.ensure_upload_budget()
    except youtube_up.QuotaExceeded as e:
        raise HTTPException(429, str(e))
    # refuse Shorts-ineligible files up-front so the UI gets an instant, clear error
    if config.YT_CHANNELS[req.channel].get("shorts_only"):
        try:
            youtube_up.ensure_shorts_eligible(path)
        except RuntimeError as e:
            raise HTTPException(400, str(e))
    upload_key = (req.kind, req.file, req.channel)
    with _uploads_lock:  # block double-clicks / repeat submissions
        if upload_key in ACTIVE_UPLOADS:
            raise HTTPException(409, f"'{req.file}' is already uploading to this "
                                     f"channel — check the upload status toast.")
        ACTIVE_UPLOADS.add(upload_key)
    job = Job("youtube")
    label = config.YT_CHANNELS[req.channel]["label"]

    def do(job):
        try:
            job.log(f"channel: {label} | privacy: {req.privacy}")
            job.set(0.05, f"uploading to '{label}' — if a Google sign-in opens, "
                          f"pick THAT channel in the account chooser")
            res = youtube_up.upload(path, req.title, req.description, req.hashtags,
                                    req.privacy, channel_key=req.channel)
            job.result = res
            # stamp the library metadata so the card shows an "on YouTube" badge
            mf = path.with_suffix(".json")
            try:
                meta = json.loads(mf.read_text(encoding="utf-8")) if mf.exists() else {}
            except json.JSONDecodeError:
                meta = {}
            meta["youtube"] = {"url": res["url"], "video_id": res["video_id"],
                               "channel": res["channel"], "privacy": req.privacy,
                               "uploaded_at": time.strftime("%Y-%m-%d %H:%M")}
            mf.write_text(json.dumps(meta, indent=2), encoding="utf-8")
            job.set(1.0, f"uploaded to {res['channel']} ({req.privacy}): {res['url']} — "
                         f"{res['uploads_left_today']} free upload(s) left today")
        finally:
            with _uploads_lock:
                ACTIVE_UPLOADS.discard(upload_key)
    return _spawn(job, do)


# ------------------------------------------------------------------ static

app.mount("/media/clips", StaticFiles(directory=config.CLIPS_DIR), name="clips")
app.mount("/media/renders", StaticFiles(directory=config.RENDERS_DIR), name="renders")


@app.get("/")
def index():
    return FileResponse(config.ROOT / "frontend" / "index.html")


app.mount("/static", StaticFiles(directory=config.ROOT / "frontend"), name="static")

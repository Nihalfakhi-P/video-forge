"""Video ingestion: YouTube / Google Drive links via yt-dlp, plus direct uploads.
Also fetches captions (free transcript source) when available."""
import json
import re
import time
from pathlib import Path

from . import config


def _safe_name(name: str) -> str:
    return re.sub(r"[^\w\-. ]", "_", name)[:80].strip() or f"video_{int(time.time())}"


def is_url(text: str) -> bool:
    return bool(re.match(r"https?://", text.strip(), re.I))


def download(url: str, progress=None) -> dict:
    """Download a video (YouTube, Google Drive share link, or any yt-dlp-supported
    URL) into the ingest dir. Returns {path, title, transcript}."""
    import yt_dlp

    def hook(d):
        if progress and d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            if total:
                progress(min(0.99, d.get("downloaded_bytes", 0) / total),
                         "downloading source video")

    from .media_utils import ffmpeg_exe

    url = re.sub(r"(?i)^http://", "https://", url.strip())
    # free space BEFORE downloading: drop every old source except this video's
    # own files (so a retry can resume), then verify there is room to work
    m = re.search(r"(?:v=|youtu\.be/|/shorts/)([\w-]{11})", url)
    _purge_ingest_except(m.group(1) if m else None)

    out_tmpl = str(config.INGEST_DIR / "%(title).60s-%(id)s.%(ext)s")
    opts = {
        "outtmpl": out_tmpl,
        "ffmpeg_location": ffmpeg_exe(),
        # Up to 1080p for output quality; prefer 30fps (Shorts render at 30fps and
        # 60fps gameplay files are ~2x larger) and h.264: AV1/VP9 software-decode
        # is many times slower on this machine, and the GTX 1650 hw-decodes h.264.
        "format": ("bv*[height<=1080][fps<=30][vcodec^=avc1]+ba/"
                   "bv*[height<=1080][vcodec^=avc1]+ba/"
                   "bv*[height<=1080][fps<=30][vcodec^=vp9]+ba/"
                   "bv*[height<=1080][vcodec^=vp9]+ba/"
                   "bv*[height<=1080]+ba/b[height<=1080]/b"),
        "merge_output_format": "mp4",
        "writethumbnail": True,  # used to detect & skip embedded title-card frames
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": ["en", "en-orig", "en-US"],
        "subtitlesformat": "json3/vtt",
        "noplaylist": True,
        "progress_hooks": [hook],
        "quiet": True,
        "no_warnings": True,
        "retries": 5,
        "fragment_retries": 5,
    }
    # Automatic proxy fallback: direct connection first; when a download fails in
    # a way that smells like ISP/geo blocking and DOWNLOAD_PROXY is configured,
    # retry through the proxy — and remember the host so later downloads from
    # that site go straight to the proxy (until the server restarts).
    from urllib.parse import urlparse
    host = (urlparse(url).hostname or "").removeprefix("www.")
    if config.DOWNLOAD_PROXY and host in _PROXIED_HOSTS:
        proxy_modes: list[str | None] = [config.DOWNLOAD_PROXY]
    elif config.DOWNLOAD_PROXY:
        proxy_modes = [None, config.DOWNLOAD_PROXY]
    else:
        proxy_modes = [None]

    # YouTube intermittently 403s one player client while others still work;
    # walk through alternate clients before giving up.
    client_attempts: list[list[str] | None] = [None, ["android"], ["ios"]]
    info, last_err, path = None, None, None
    for mi, proxy in enumerate(proxy_modes):
        for clients in client_attempts:
            o = dict(opts)
            if proxy:
                o["proxy"] = proxy
            if clients:
                o["extractor_args"] = {"youtube": {"player_client": clients}}
            try:
                with yt_dlp.YoutubeDL(o) as ydl:
                    probe = ydl.extract_info(url, download=False)
                    _check_disk_for(probe)  # fail fast, sized to the actual video
                    info = ydl.extract_info(url, download=True)
                    path = Path(ydl.prepare_filename(info)).with_suffix(".mp4")
                break
            except yt_dlp.utils.DownloadError as e:
                last_err = e
                if "403" in str(e) or "Forbidden" in str(e):
                    if progress:
                        progress(0.0, f"HTTP 403 — retrying via {clients or ['web']} client")
                    continue  # next player client
                break  # different failure: leave client loop, maybe try proxy
        if info is not None:
            if proxy:
                _PROXIED_HOSTS.add(host)
            break
        is_last = mi == len(proxy_modes) - 1
        if not is_last and _looks_blocked(last_err):
            if progress:
                progress(0.0, "direct connection failed (looks blocked) — "
                              "retrying through DOWNLOAD_PROXY")
        elif not is_last:
            break  # not block-shaped: the proxy won't help
    if info is None:
        raise RuntimeError(
            f"Download failed{' (also via proxy)' if len(proxy_modes) > 1 else ''}: "
            f"{last_err}. If this site is blocked by your ISP, set DOWNLOAD_PROXY "
            f"in .env — blocked sites then retry through it automatically.")
    if not path.exists():  # merge may keep original ext
        cands = sorted(config.INGEST_DIR.glob(f"*{info['id']}*"),
                       key=lambda p: p.stat().st_size, reverse=True)
        vids = [c for c in cands if c.suffix in (".mp4", ".mkv", ".webm", ".mov")]
        if not vids:
            raise RuntimeError("Download finished but video file not found")
        path = vids[0]
    transcript = _load_captions(info["id"])
    thumb = next((p for p in config.INGEST_DIR.glob(f"*{info['id']}*")
                  if p.suffix in (".webp", ".jpg", ".jpeg", ".png")), None)
    return {"path": str(path), "title": info.get("title", path.stem),
            "transcript": transcript, "thumbnail": str(thumb) if thumb else ""}


def _load_captions(video_id: str) -> list[dict]:
    """Parse downloaded captions into [{t: seconds, text: ...}]."""
    for f in config.INGEST_DIR.glob(f"*{video_id}*.json3"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            out = []
            for ev in data.get("events", []):
                text = "".join(seg.get("utf8", "") for seg in ev.get("segs", [])).strip()
                if text and text != "\n":
                    out.append({"t": ev.get("tStartMs", 0) / 1000.0, "text": text})
            if out:
                return out
        except (json.JSONDecodeError, OSError):
            continue
    for f in config.INGEST_DIR.glob(f"*{video_id}*.vtt"):
        return _parse_vtt(f)
    return []


def _parse_vtt(path: Path) -> list[dict]:
    out, t = [], 0.0
    ts = re.compile(r"(\d+):(\d+):(\d+(?:\.\d+)?)\s*-->")
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = ts.match(line.strip())
        if m:
            h, mnt, s = m.groups()
            t = int(h) * 3600 + int(mnt) * 60 + float(s)
        elif line.strip() and "-->" not in line and not line.startswith(("WEBVTT", "Kind:", "Language:")):
            text = re.sub(r"<[^>]+>", "", line).strip()
            if text and (not out or out[-1]["text"] != text):
                out.append({"t": t, "text": text})
    return out


# hosts that needed the proxy this session — future downloads skip the direct try
_PROXIED_HOSTS: set[str] = set()


def _looks_blocked(err) -> bool:
    """Does this failure look like ISP/geo blocking rather than a bad URL?"""
    s = str(err or "").lower()
    return any(t in s for t in (
        "getaddrinfo", "timed out", "timeout", "connection", "reset",
        "refused", "unreachable", "geo", "not available in your",
        "unable to download webpage", "ssl", "eof occurred"))


def _check_disk_for(info: dict):
    """Refuse the download early if the disk can't hold it: the video is stored
    once, merged once (x2), plus headroom for clips and temp files."""
    import shutil
    fmts = info.get("requested_formats") or [info]
    est = sum((f.get("filesize") or f.get("filesize_approx") or 0) for f in fmts)
    free = shutil.disk_usage(str(config.INGEST_DIR))[2]
    need = max(800_000_000, est * 3 + 500_000_000)
    if free < need:
        raise RuntimeError(
            f"Not enough disk space: video is ~{est / 1e9:.1f} GB and processing "
            f"needs ~{need / 1e9:.1f} GB, but only {free / 1e9:.1f} GB is free. "
            f"Free up space and try again.")


def _purge_ingest_except(video_id: str | None):
    """Delete all cached sources except files of the given video id (lets a
    retried download resume its own partials). Runs BEFORE downloading so a
    full disk can't kill the merge step."""
    if not config.AUTO_CLEAN_INGEST:
        return
    for f in config.INGEST_DIR.iterdir():
        if f.is_file() and not (video_id and video_id in f.name):
            try:
                f.unlink()
            except OSError:
                pass


def cleanup_old_sources(keep: str | Path) -> int:
    """Delete previously downloaded/uploaded source videos (and their caption
    files), keeping only the source currently being processed. Produced clips
    and renders are never touched. Returns bytes freed."""
    if not config.AUTO_CLEAN_INGEST:
        return 0
    keep = Path(keep)
    freed = 0
    for f in config.INGEST_DIR.iterdir():
        if not f.is_file() or f.stem.startswith(keep.stem[:20]) or f == keep:
            continue
        try:
            size = f.stat().st_size
            f.unlink()
            freed += size
        except OSError:
            pass  # in use by another running job
    return freed


def save_upload(filename: str, data: bytes) -> dict:
    path = config.INGEST_DIR / _safe_name(filename)
    path.write_bytes(data)
    return {"path": str(path), "title": path.stem, "transcript": []}


def transcript_window(transcript: list[dict], start: float, end: float, pad: float = 25) -> str:
    """Format the caption lines around [start, end] with [second] markers."""
    lines = [f"[{int(c['t'])}s] {c['text']}" for c in transcript
             if start - pad <= c["t"] <= end + pad]
    return "\n".join(lines)[:3500]


def transcript_full_text(transcript: list[dict]) -> str:
    return " ".join(c["text"] for c in transcript)

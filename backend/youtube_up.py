"""YouTube Shorts upload via the YouTube Data API v3 — multi-channel + quota-aware.

One-time setup (free): Google Cloud project -> enable "YouTube Data API v3" ->
OAuth consent screen (add yourself as test user) -> OAuth client ID (Desktop app)
-> save the JSON as client_secret.json in the project root.

Channels: the same Google account owns multiple channels (personal + Brand
Accounts). Each channel key in config.YT_CHANNELS keeps its own token file. On a
channel's first upload, Google's account chooser opens in the browser — pick the
matching channel there; the token is then bound to it forever.

Quota: free tier = 10,000 units/day, an upload costs 1,600 units => 6 uploads/day,
resetting at midnight US-Pacific. Usage is tracked locally in yt_quota.json and
uploads are refused (with a clear message) once the day's budget is spent.
"""
import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config

SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube.readonly"]
PACIFIC = ZoneInfo("America/Los_Angeles")


def is_configured() -> bool:
    return config.YT_CLIENT_SECRET.exists()


# ------------------------------------------------------------------ quota

def _pacific_today() -> str:
    return datetime.now(PACIFIC).strftime("%Y-%m-%d")

def _load_quota() -> dict:
    try:
        q = json.loads(config.YT_QUOTA_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        q = {}
    if q.get("date") != _pacific_today():
        q = {"date": _pacific_today(), "used_units": 0, "uploads": 0}
    return q

def _charge(units: int, upload: bool = False):
    q = _load_quota()
    q["used_units"] += units
    if upload:
        q["uploads"] += 1
    config.YT_QUOTA_FILE.write_text(json.dumps(q, indent=2), encoding="utf-8")

def quota_status() -> dict:
    q = _load_quota()
    left_units = max(0, config.YT_DAILY_UNITS - q["used_units"])
    reset_pacific = (datetime.now(PACIFIC) + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    reset_local = reset_pacific.astimezone()
    return {
        "date_pacific": q["date"],
        "uploads_today": q["uploads"],
        "uploads_left": left_units // config.YT_UPLOAD_COST,
        "max_uploads_per_day": config.YT_DAILY_UNITS // config.YT_UPLOAD_COST,
        "used_units": q["used_units"],
        "daily_units": config.YT_DAILY_UNITS,
        "resets_at_local": reset_local.strftime("%Y-%m-%d %H:%M"),
    }


class QuotaExceeded(RuntimeError):
    pass


def ensure_upload_budget():
    st = quota_status()
    if st["uploads_left"] < 1:
        raise QuotaExceeded(
            f"Daily free YouTube quota is exhausted: {st['uploads_today']}/"
            f"{st['max_uploads_per_day']} uploads used today. The free tier allows "
            f"{st['max_uploads_per_day']} uploads/day (10,000 units, 1,600 per upload). "
            f"It resets at {st['resets_at_local']} your time (midnight US-Pacific).")


# ------------------------------------------------------------------ channels

def _channel_cfg(channel_key: str) -> dict:
    cfg = config.YT_CHANNELS.get(channel_key)
    if not cfg:
        raise RuntimeError(f"Unknown channel '{channel_key}'. "
                           f"Options: {', '.join(config.YT_CHANNELS)}")
    return cfg


def _load_channel_info() -> dict:
    try:
        return json.loads(config.YT_CHANNEL_INFO.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_channel_info(info: dict):
    config.YT_CHANNEL_INFO.write_text(json.dumps(info, indent=2), encoding="utf-8")


def channels_status() -> list[dict]:
    info = _load_channel_info()
    out = []
    for key, cfg in config.YT_CHANNELS.items():
        out.append({
            "key": key,
            "label": cfg["label"],
            "made_for_kids": cfg["made_for_kids"],
            "authorized": Path(cfg["token"]).exists(),
            "channel_title": info.get(key, {}).get("title", ""),
            "channel_id": info.get(key, {}).get("id", ""),
        })
    return out


def _service(channel_key: str):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    cfg = _channel_cfg(channel_key)
    token_file = Path(cfg["token"])
    creds = None
    if token_file.exists():
        creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception:
                # invalid_grant: Google expires refresh tokens after 7 days while
                # the OAuth app is in "Testing" mode. Treat as not-authorized and
                # re-run the browser flow instead of failing the upload.
                creds = None
                token_file.unlink(missing_ok=True)
                info = _load_channel_info()
                info.pop(channel_key, None)
                _save_channel_info(info)
        if not creds or not creds.valid:
            flow = InstalledAppFlow.from_client_secrets_file(str(config.YT_CLIENT_SECRET), SCOPES)
            # Browser opens: user must pick the channel matching `channel_key`
            creds = flow.run_local_server(
                port=0, authorization_prompt_message="",
                success_message=f"Authorized for '{cfg['label']}'. You can close this tab.")
        token_file.write_text(creds.to_json(), encoding="utf-8")
    yt = build("youtube", "v3", credentials=creds)
    # cache which channel this token actually targets (1 quota unit, once)
    info = _load_channel_info()
    if channel_key not in info:
        resp = yt.channels().list(part="snippet", mine=True).execute()
        _charge(1)
        items = resp.get("items", [])
        if items:
            info[channel_key] = {"title": items[0]["snippet"]["title"], "id": items[0]["id"]}
            config.YT_CHANNEL_INFO.write_text(json.dumps(info, indent=2), encoding="utf-8")
    return yt


def ensure_shorts_eligible(video_path: Path):
    """YouTube has no 'upload as Short' flag — it auto-classifies verticals under
    3 minutes. Enforce both conditions so a clip never lands as a regular video."""
    from .media_utils import probe_dimensions, probe_duration

    w, h = probe_dimensions(video_path)
    dur = probe_duration(video_path)
    problems = []
    if h <= w:
        problems.append(f"not vertical ({w}x{h})")
    if dur > config.SHORTS_MAX_SECONDS:
        problems.append(f"too long for a Short ({dur:.0f}s > {config.SHORTS_MAX_SECONDS}s)")
    if problems:
        raise RuntimeError(
            "Refused: this video would NOT be classified as a Short — "
            + " and ".join(problems)
            + ". Re-cut it shorter/vertical or disable shorts_only in config.py.")


def upload(video_path: Path, title: str, description: str, tags: list[str],
           privacy: str = "public", channel_key: str = config.DEFAULT_CHANNEL) -> dict:
    """Upload as a Short to the chosen channel. madeForKids and Shorts
    enforcement are set per channel config."""
    from googleapiclient.http import MediaFileUpload

    if not is_configured():
        raise RuntimeError("client_secret.json missing — see youtube_up.py docstring for setup")
    ensure_upload_budget()
    cfg = _channel_cfg(channel_key)
    if cfg.get("shorts_only"):
        ensure_shorts_eligible(video_path)
    # #Shorts in BOTH title and description maximizes correct shelf placement
    if "#shorts" not in title.lower() and len(title) <= 91:
        title = f"{title} #Shorts"
    if "#shorts" not in description.lower():
        description = (description + "\n\n#Shorts").strip()
    yt = _service(channel_key)
    body = {
        "snippet": {
            "title": title[:100],
            "description": description[:4900],
            "tags": [t.lstrip("#") for t in tags][:15],
            "categoryId": config.YT_CATEGORY_ID,
        },
        "status": {"privacyStatus": privacy,
                   "selfDeclaredMadeForKids": cfg["made_for_kids"]},
    }
    media = MediaFileUpload(str(video_path), chunksize=1024 * 1024 * 4, resumable=True)
    req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    _charge(config.YT_UPLOAD_COST, upload=True)
    st = quota_status()
    info = _load_channel_info().get(channel_key, {})
    return {"video_id": resp["id"], "url": f"https://youtube.com/shorts/{resp['id']}",
            "privacy": privacy, "channel": info.get("title") or cfg["label"],
            "uploads_left_today": st["uploads_left"]}

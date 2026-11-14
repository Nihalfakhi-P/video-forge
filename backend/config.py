"""Central configuration for VideoForge."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Load ROOT/.env (KEY=VALUE lines) into the environment before any reads below.
# Real environment variables win over .env values.
_env_file = ROOT / ".env"
if _env_file.exists():
    for _line in _env_file.read_text(encoding="utf-8-sig").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))
MEDIA = ROOT / "media"
INGEST_DIR = MEDIA / "ingest"      # downloaded / uploaded source videos
CLIPS_DIR = MEDIA / "clips"        # repurposed output clips
RENDERS_DIR = MEDIA / "renders"    # generated animation output
FRAMES_DIR = MEDIA / "frames"      # generated scene images / temp assets
MODELS_DIR = ROOT / "models"       # local AI models (depth, whisper, ...)

for d in (INGEST_DIR, CLIPS_DIR, RENDERS_DIR, FRAMES_DIR, MODELS_DIR):
    d.mkdir(parents=True, exist_ok=True)

# ---- LLM ----
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.2")
# Optional free-tier cloud fallbacks (used only if Ollama is unreachable)
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.1-8b-instant")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")

# ---- Tier 1 heuristics ----
AUDIO_SR = 16000                # decode rate for RMS analysis
RMS_FRAME_SEC = 0.50            # RMS window size
MOTION_SAMPLE_FPS = 2.0         # optical-flow sampling rate
MOTION_DOWNSCALE_W = 160        # analysis resolution (speed)
SEGMENT_MIN_SEC = 25            # candidate clip bounds
SEGMENT_MAX_SEC = 55
MIN_OUTPUT_SECONDS = 25         # every produced short (clip or animation) is at
                                # least this long (unless the source itself is shorter)
MAX_CANDIDATES = 6              # candidates passed to the LLM

# ---- Rendering ----
SHORT_W, SHORT_H = 1080, 1920   # 9:16 full HD (YouTube gives verticals better
                                # bitrate when the upload is 1080p+)
CRF = 19                        # x264 quality (lower = better); 19 ≈ visually lossless.
                                # CRF sets quality; preset only trades encode speed vs
                                # file size at that quality. "fast" keeps this laptop snappy.
X264_PRESET = "fast"
# Burn the clip title ("Part 1: ...") into the top of the video. Off by default:
# the title still goes into the YouTube title/description, just not on screen.
BURN_TITLES = os.environ.get("BURN_TITLES", "0").lower() in ("1", "true", "yes")

# ---- Download proxy (optional) ----
# Routes ONLY video ingest (yt-dlp) through a proxy — YouTube uploads, TTS and
# image generation stay on the direct connection. Useful for sources that are
# ISP-blocked locally (e.g. Bilibili in India): set an http:// or socks5:// URL
# from your VPN provider's proxy credentials, e.g. socks5://user:pass@host:1080
DOWNLOAD_PROXY = os.environ.get("DOWNLOAD_PROXY", "")
EDGE_TTS_VOICE = os.environ.get("EDGE_TTS_VOICE", "en-US-ChristopherNeural")
# energetic commentator voice for optional clip reaction voiceovers (Workflow A)
REACTION_TTS_VOICE = os.environ.get("REACTION_TTS_VOICE", "en-US-GuyNeural")
# clear narrator voice for full-length English commentary over foreign-audio clips
COMMENTARY_TTS_VOICE = os.environ.get("COMMENTARY_TTS_VOICE", "en-US-AndrewNeural")

# ---- YouTube upload ----
YT_CLIENT_SECRET = ROOT / "client_secret.json"   # user supplies from Google Cloud Console
YT_CATEGORY_ID = "24"  # Entertainment

# Upload channels. Each gets its own OAuth token file: during the first upload to
# a channel, Google's chooser opens in the browser and the user must pick THAT
# channel — the token then permanently targets it.
# Default: one channel. To use several channels under one Google account, create
# channels.json (git-ignored, see channels.example.json), e.g.
#   {"kids": {"label": "Kids", "made_for_kids": true}, "gaming": {"label": "Gaming"}}
YT_CHANNELS = {
    "main": {
        "label": "My YouTube channel",
        "token": ROOT / "yt_token_main.json",
        "made_for_kids": False,
        "shorts_only": True,   # refuse uploads that wouldn't classify as Shorts
    },
}
_channels_file = ROOT / "channels.json"
if _channels_file.exists():
    import json as _json
    YT_CHANNELS = {
        _key: {
            "label": _c.get("label", _key),
            "token": ROOT / f"yt_token_{_key}.json",
            "made_for_kids": bool(_c.get("made_for_kids", False)),
            "shorts_only": bool(_c.get("shorts_only", True)),
        }
        for _key, _c in _json.loads(_channels_file.read_text(encoding="utf-8-sig")).items()
    }
DEFAULT_CHANNEL = next(iter(YT_CHANNELS))
# YouTube auto-classifies a Short: vertical/square AND under 3 minutes.
SHORTS_MAX_SECONDS = 178  # safety margin below the 180s limit
YT_CHANNEL_INFO = ROOT / "yt_channel_info.json"  # cached channel names after auth

# Free-tier quota: 10,000 units/day, videos.insert = 1,600 units -> 6 uploads/day.
# Resets midnight US-Pacific. Tracked locally in YT_QUOTA_FILE.
YT_DAILY_UNITS = 10_000
YT_UPLOAD_COST = 1_600
YT_QUOTA_FILE = ROOT / "yt_quota.json"

# ---- housekeeping ----
# Delete previously downloaded source videos when a new job starts (keeps only
# the source currently being processed; produced clips are never touched).
AUTO_CLEAN_INGEST = True

# optional private add-on settings (not in the public repo)
try:
    from .config_generate import *  # noqa: F401,F403
except ImportError:
    pass

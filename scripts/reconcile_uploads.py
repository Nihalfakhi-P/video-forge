"""One-off: stamp library items that were uploaded to YouTube before upload
tracking existed. Reads each authorized channel's uploads playlist (cheap:
~2 quota units total) and matches video titles to library metadata titles."""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import config, youtube_up


def norm(t: str) -> str:
    t = re.sub(r"#\w+", "", t or "")
    return re.sub(r"[^a-z0-9]", "", t.lower())


def main():
    uploads = {}  # norm(title) -> (video_id, channel_title, published)
    for key, cfg in config.YT_CHANNELS.items():
        if not Path(cfg["token"]).exists():
            continue
        yt = youtube_up._service(key)
        ch = yt.channels().list(part="contentDetails,snippet", mine=True).execute()
        youtube_up._charge(1)
        item = ch["items"][0]
        ch_title = item["snippet"]["title"]
        pl = item["contentDetails"]["relatedPlaylists"]["uploads"]
        resp = yt.playlistItems().list(part="snippet", playlistId=pl, maxResults=50).execute()
        youtube_up._charge(1)
        for v in resp.get("items", []):
            sn = v["snippet"]
            uploads[norm(sn["title"])] = (sn["resourceId"]["videoId"], ch_title,
                                          sn.get("publishedAt", "")[:16].replace("T", " "))
        print(f"{ch_title}: {len(resp.get('items', []))} uploads fetched")

    stamped = 0
    for folder in (config.CLIPS_DIR, config.RENDERS_DIR):
        for mf in folder.glob("*.json"):
            meta = json.loads(mf.read_text(encoding="utf-8-sig"))
            if meta.get("youtube") or not meta.get("title"):
                continue
            hit = uploads.get(norm(meta["title"]))
            if hit:
                vid, ch_title, published = hit
                meta["youtube"] = {"url": f"https://youtube.com/shorts/{vid}",
                                   "video_id": vid, "channel": ch_title,
                                   "privacy": "unknown", "uploaded_at": published}
                mf.write_text(json.dumps(meta, indent=2), encoding="utf-8")
                print(f"stamped: {mf.name} -> {vid} ({ch_title})")
                stamped += 1
    print(f"done, {stamped} stamped")


if __name__ == "__main__":
    main()

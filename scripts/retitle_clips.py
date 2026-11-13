"""One-off: re-cut existing library clips with their titles burned into the frame.
Matches each clip's metadata to its cached source in media/ingest."""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import config
from backend.media_utils import cut_vertical_clip


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def find_source(source_title: str) -> Path | None:
    vids = [p for p in config.INGEST_DIR.glob("*.mp4")]
    key = norm(source_title)
    for v in vids:
        stem = norm(re.sub(r"-[\w-]{11}$", "", v.stem))  # drop yt id suffix
        if stem and (stem in key or key in stem or stem[:25] == key[:25]):
            return v
    return None


def main():
    done = skipped = 0
    for meta_file in sorted(config.CLIPS_DIR.glob("*.json")):
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        clip = config.CLIPS_DIR / meta["file"]
        title = (meta.get("title") or "").strip()
        if not clip.exists() or not title:
            skipped += 1
            continue
        sp = meta.get("source_path", "")
        src = Path(sp) if sp else None
        if not (src and src.is_file()):
            src = find_source(meta.get("source", ""))
        if not (src and src.is_file()):
            print(f"SKIP (no source): {meta['file']}")
            skipped += 1
            continue
        print(f"re-cutting {meta['file']}  <-  {title!r}")
        cut_vertical_clip(src, float(meta["start"]), float(meta["end"]), clip, title=title)
        meta["source_path"] = str(src)
        meta_file.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        done += 1
    print(f"done: {done} re-cut, {skipped} skipped")


if __name__ == "__main__":
    main()

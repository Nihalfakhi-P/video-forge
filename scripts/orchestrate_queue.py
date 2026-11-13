"""Autonomous queue driver for repurposing a list of Bilibili videos into shorts,
ONE AT A TIME (8GB laptop can't run two whisper+scan jobs at once).

Each invocation advances the queue by at most one step:
  - if a job is running -> report and exit (wait)
  - if the last job finished -> mark done/error, record its clips
  - if idle and something is pending -> start the next one
Prints a status line the agent can read; safe to call repeatedly.
"""
import json
import sys
import time
from pathlib import Path
from urllib import request as _rq

QUEUE_FILE = Path(sys.argv[1])
API = "http://127.0.0.1:8321"


def _get(path):
    with _rq.urlopen(API + path, timeout=15) as r:
        return json.loads(r.read())


def _post(path, body):
    req = _rq.Request(API + path, data=json.dumps(body).encode(),
                      headers={"Content-Type": "application/json"}, method="POST")
    with _rq.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def main():
    q = json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
    items = q["queue"]

    running = next((it for it in items if it["status"] == "running"), None)
    if running:
        try:
            j = _get(f"/api/jobs/{running['job']}")
        except Exception as e:
            print(f"WAIT running {running['label']} (status unreachable: {e})")
            return
        if j["status"] in ("running", "queued"):
            print(f"WAIT {running['label']}: {j['status']} "
                  f"{round(j['progress'] * 100)}% — {j['message']}")
            return
        # finished
        if j["status"] == "error":
            running["status"] = "error"
            running["error"] = j.get("error", "")
            print(f"ERROR {running['label']}: {j.get('error','')[:120]}")
        else:
            running["status"] = "done"
            clips = (j.get("result") or {}).get("clips", [])
            running["clips"] = [{"file": c["file"], "title": c["title"],
                                 "start": c["start"], "end": c["end"]} for c in clips]
            print(f"DONE {running['label']}: {len(clips)} clip(s)")
        QUEUE_FILE.write_text(json.dumps(q, indent=2), encoding="utf-8")

    # start next pending
    nxt = next((it for it in items if it["status"] == "pending"), None)
    if not nxt:
        done = [it for it in items if it["status"] == "done"]
        total = sum(len(it.get("clips", [])) for it in items)
        print(f"ALL_DONE {len(done)}/{len(items)} videos ok, {total} clips total")
        return

    body = {"url": f"https://www.bilibili.com/video/{nxt['bv']}",
            "reaction_vo": False, "commentary_vo": False,
            "skip_thumbnail": True,
            "frame": q.get("frame", "fit")}  # batch-level: "fit" | "fill" (full-screen)
    try:
        r = _post("/api/repurpose/url", body)
        nxt["job"] = r["job_id"]
        nxt["status"] = "running"
        nxt["started"] = time.strftime("%H:%M:%S")
        QUEUE_FILE.write_text(json.dumps(q, indent=2), encoding="utf-8")
        print(f"STARTED {nxt['label']} -> job {r['job_id']}")
    except Exception as e:
        print(f"START_FAIL {nxt['label']}: {e}")


if __name__ == "__main__":
    main()

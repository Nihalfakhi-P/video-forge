"""Workflow A: hybrid heuristic + LLM repurposing pipeline.

ingest -> thumbnail-frame ban -> Tier 1 (RMS + optical flow) -> Tier 2 (Ollama
verify/adjust/metadata) -> FFmpeg vertical cut (fit or fill) [+ optional reaction
voiceover] -> clip files + metadata JSON in storyline order (Part 1..n).
"""
import asyncio
import json
import tempfile
import time
from dataclasses import asdict
from pathlib import Path

from . import config, ingest, llm, tier1
from .media_utils import (cut_vertical_clip, mix_commentary,
                          mix_reaction_voiceover, probe_duration)


def run(job, source: str | dict, options: dict | None = None):
    """source: URL string, or {"path":..., "title":..., "transcript":[...]} for uploads.
    options: {reaction_vo, commentary_vo, skip_thumbnail, frame}"""
    opts = {"reaction_vo": False, "commentary_vo": False,
            "skip_thumbnail": True, "frame": "fit", **(options or {})}
    job.set(0.02, "ingesting source")
    if isinstance(source, str):
        info = ingest.download(source, progress=lambda p, m: job.set(0.02 + p * 0.18, m))
    else:
        info = source
    src = Path(info["path"])
    transcript = info.get("transcript") or []
    job.log(f"source ready: {src.name}"
            + (f" ({len(transcript)} caption lines)" if transcript else " (no captions found)"))
    if not transcript:
        # no captions (Bilibili, raw uploads, ...): listen to the audio and
        # translate it to English so Tier 2 gets real context
        from . import transcribe
        dur_probe = tier1.probe_duration(src)
        if dur_probe <= transcribe.MAX_TRANSCRIBE_SECONDS:
            job.set(0.16, "no captions — transcribing audio to English (whisper)")
            transcript = transcribe.translate_to_english(
                src, dur_probe,
                progress=lambda p: job.set(0.16 + p * 0.04,
                                           "transcribing audio to English (whisper)"))
            if transcript:
                job.log(f"whisper produced {len(transcript)} English transcript lines")
            else:
                job.log("whisper transcription unavailable — continuing without transcript")
        else:
            job.log("source too long for whisper transcription — skipping")
    freed = ingest.cleanup_old_sources(src)
    if freed:
        job.log(f"storage: deleted old source video(s), freed {freed / 1e6:.0f} MB")

    job.set(0.2, "Tier 1: scanning for high-energy segments")
    src_dur = tier1.probe_duration(src)
    if src_dur > 7200:
        job.log(f"note: source is {src_dur / 3600:.1f}h long — the Tier 1 scan "
                f"will take a while (memory stays flat, but compute scales with length)")
    banned = None
    if opts["skip_thumbnail"] and info.get("thumbnail"):
        banned = tier1.thumbnail_banned_seconds(
            src, info["thumbnail"], tier1.probe_duration(src))
        if banned is not None and banned.mean() > 0.5:
            # the "thumbnail" IS the video (static-image upload / matching art):
            # banning it all would yield zero clips, so the filter steps aside
            job.log("thumbnail matches most of the video — skip-thumbnail filter disabled")
            banned = None
        elif banned is not None:
            job.log(f"thumbnail card detected in video at seconds "
                    f"{[int(s) for s in banned.nonzero()[0][:20]]} — excluded from clips")
    candidates, duration = tier1.find_candidates(
        src, progress=lambda p, m: job.set(0.2 + p * 0.3, f"Tier 1: {m}"), banned=banned)
    job.log(f"Tier 1 found {len(candidates)} candidate segments in {duration:.0f}s video")

    llm_info = llm.backend_status()
    job.log(f"Tier 2 backend: {llm_info['detail']}")

    # ---- phase 1: Tier 2 verifies every candidate (kept ones stay chronological)
    kept = []
    for i, cand in enumerate(candidates):
        job.set(0.5 + 0.25 * i / max(1, len(candidates)),
                f"Tier 2: verifying segment {i + 1}/{len(candidates)} "
                f"({cand.start:.0f}s-{cand.end:.0f}s)")
        window = ingest.transcript_window(transcript, cand.start, cand.end)
        verdict = llm.verify_candidate(asdict(cand), window, info["title"])
        if not verdict.get("keep"):
            job.log(f"segment {i + 1} dropped by LLM: {verdict.get('reason', '')}")
            continue
        s, e = float(verdict["start"]), min(float(verdict["end"]), duration)
        if e - s < config.MIN_OUTPUT_SECONDS:  # source-end clamp can shorten: shift start back
            s = max(0.0, e - config.MIN_OUTPUT_SECONDS)
        if banned is not None and banned[int(s):int(e)].any():
            s, e = cand.start, cand.end  # LLM drifted into thumbnail frames: revert
        cand.title = verdict.get("title", "")
        cand.description = verdict.get("description", "")
        cand.hashtags = verdict.get("hashtags", [])
        cand.reason = verdict.get("reason", "")
        cand.transcript = window
        kept.append((s, e, cand))

    # ---- phase 2: cut in playback order, labeled Part 1..n so the source's
    # storyline carries across the Shorts
    n = len(kept)
    results = []
    used_vo_lines: list[str] = []
    for k, (s, e, cand) in enumerate(kept, start=1):
        part_title = f"Part {k}: {cand.title}" if n > 1 and cand.title else cand.title
        job.set(0.75 + 0.24 * (k - 1) / max(1, n),
                f"cutting clip {k}/{n} ({s:.0f}s-{e:.0f}s)")
        stamp = int(time.time())
        out = config.CLIPS_DIR / f"clip_{stamp}_{k}.mp4"
        cut_vertical_clip(src, s, e, out, blur_pad=(opts["frame"] == "fit"),
                          title=part_title if config.BURN_TITLES else "")

        vo_line = ""
        commentary = ""
        if opts["commentary_vo"]:
            # full English narration over the whole clip — the go-to for
            # foreign-audio / no-subtitle sources like Bilibili
            job.set(job.progress, f"clip {k}/{n}: writing English commentary")
            clip_len = probe_duration(out)
            commentary = llm.commentary_script(
                cand.title or info["title"], cand.transcript, clip_len)
            vo_mp3 = Path(tempfile.gettempdir()) / f"com_{stamp}_{k}.mp3"
            try:
                asyncio.run(_tts_commentary(commentary, vo_mp3))
                mixed = out.with_name(out.stem + "_com.mp4")
                mix_commentary(out, vo_mp3, mixed)
                mixed.replace(out)
                job.log(f"clip {k}: commentary ({len(commentary.split())} words) — "
                        f"\"{commentary[:80]}…\"")
            except Exception as exc:  # never lose the clip over a voiceover
                job.log(f"clip {k}: commentary failed ({exc}) — kept clean clip")
                commentary = ""
            finally:
                vo_mp3.unlink(missing_ok=True)
        elif opts["reaction_vo"]:
            job.set(job.progress, f"clip {k}/{n}: generating reaction voiceover")
            vo_line = llm.reaction_line(cand.title or info["title"], cand.transcript,
                                        part=k, avoid=used_vo_lines)
            used_vo_lines.append(vo_line)
            vo_mp3 = Path(tempfile.gettempdir()) / f"vo_{stamp}_{k}.mp3"
            try:
                asyncio.run(_tts_reaction(vo_line, vo_mp3))
                mixed = out.with_name(out.stem + "_vo.mp4")
                mix_reaction_voiceover(out, vo_mp3, mixed)
                mixed.replace(out)
                job.log(f"clip {k}: reaction VO — \"{vo_line}\"")
            except Exception as exc:  # never lose the clip over a voiceover
                job.log(f"clip {k}: reaction VO failed ({exc}) — kept clean clip")
                vo_line = ""
            finally:
                vo_mp3.unlink(missing_ok=True)

        desc = cand.description + (f"\n\nPart {k} of {n}." if n > 1 else "")
        meta = {
            "file": out.name,
            "source": info["title"],
            "source_path": str(src),
            "start": s, "end": e,
            "part": k, "parts_total": n,
            "score": round(cand.score, 3),
            "audio_score": round(cand.audio_score, 2),
            "motion_score": round(cand.motion_score, 2),
            "title": part_title,
            "description": desc,
            "hashtags": cand.hashtags,
            "reason": cand.reason,
            "frame": opts["frame"],
            "reaction_vo": vo_line,
            "commentary_vo": commentary,
            "llm_backend": llm_info["backend"],
        }
        out.with_suffix(".json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        results.append(meta)
        job.log(f"clip saved: {out.name} — {part_title or 'untitled'}")

    job.result = {"clips": results, "source_title": info["title"],
                  "candidates_found": len(candidates)}
    job.set(1.0, f"done — {len(results)} clip(s) ready" + (" in storyline order" if n > 1 else ""))


async def _tts_reaction(text: str, out: Path):
    import edge_tts
    await edge_tts.Communicate(text, config.REACTION_TTS_VOICE, rate="+12%").save(str(out))


async def _tts_commentary(text: str, out: Path):
    import edge_tts
    await edge_tts.Communicate(text, config.COMMENTARY_TTS_VOICE, rate="+6%").save(str(out))

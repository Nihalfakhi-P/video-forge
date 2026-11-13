"""Tier 2 LLM layer.

Priority: local Ollama (llama3.2) -> free cloud fallback (Groq / OpenRouter, if the
user exported an API key) -> heuristic-only degradation (pipelines still work,
metadata is templated). Everything returns plain Python structures.
"""
import json
import re

import requests

from . import config

TIMEOUT = 300


def backend_status() -> dict:
    """Which LLM backend will be used right now?"""
    try:
        r = requests.get(f"{config.OLLAMA_URL}/api/tags", timeout=2)
        if r.ok:
            models = [m["name"] for m in r.json().get("models", [])]
            has = any(m.startswith(config.OLLAMA_MODEL) for m in models)
            return {"backend": "ollama", "ready": has, "models": models,
                    "detail": f"Ollama running, model '{config.OLLAMA_MODEL}' "
                              + ("available" if has else "NOT pulled — run: ollama pull " + config.OLLAMA_MODEL)}
    except requests.RequestException:
        pass
    if config.GROQ_API_KEY:
        return {"backend": "groq", "ready": True, "detail": "Groq free tier (Ollama not running)"}
    if config.OPENROUTER_API_KEY:
        return {"backend": "openrouter", "ready": True, "detail": "OpenRouter (Ollama not running)"}
    return {"backend": "none", "ready": False,
            "detail": "No LLM available. Install Ollama (run setup_ollama.ps1) or set GROQ_API_KEY. "
                      "Pipelines run in heuristic-only mode."}


def _chat(prompt: str, system: str = "", json_mode: bool = True,
          temperature: float = 0.4) -> str:
    status = backend_status()
    if status["backend"] == "ollama" and status["ready"]:
        body = {"model": config.OLLAMA_MODEL,
                "messages": ([{"role": "system", "content": system}] if system else [])
                + [{"role": "user", "content": prompt}],
                "stream": False,
                "options": {"temperature": temperature, "num_ctx": 4096}}
        if json_mode:
            body["format"] = "json"
        r = requests.post(f"{config.OLLAMA_URL}/api/chat", json=body, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()["message"]["content"]
    if status["backend"] == "groq":
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {config.GROQ_API_KEY}"},
            json={"model": config.GROQ_MODEL,
                  "messages": ([{"role": "system", "content": system}] if system else [])
                  + [{"role": "user", "content": prompt}],
                  "temperature": 0.4,
                  **({"response_format": {"type": "json_object"}} if json_mode else {})},
            timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
    if status["backend"] == "openrouter":
        r = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {config.OPENROUTER_API_KEY}"},
            json={"model": "meta-llama/llama-3.2-3b-instruct:free",
                  "messages": ([{"role": "system", "content": system}] if system else [])
                  + [{"role": "user", "content": prompt}]},
            timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
    raise RuntimeError("no-llm")


def _json_loads_loose(text: str):
    """LLMs sometimes wrap JSON in prose/fences; extract the first JSON object/array."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    for opener, closer in (("{", "}"), ("[", "]")):
        i = text.find(opener)
        if i >= 0:
            depth = 0
            for j in range(i, len(text)):
                if text[j] == opener:
                    depth += 1
                elif text[j] == closer:
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(text[i:j + 1])
                        except json.JSONDecodeError:
                            break
    raise ValueError(f"Unparseable LLM output: {text[:200]}")


# ---------------------------------------------------------------- Workflow A

def verify_candidate(candidate: dict, transcript_window: str, video_title: str) -> dict:
    """Ask the LLM to keep/drop a Tier-1 segment, fine-tune timestamps and write metadata."""
    prompt = f"""You are a short-form video editor picking viral clips.

Source video: "{video_title}"
A heuristic pass flagged the segment {candidate['start']:.0f}s-{candidate['end']:.0f}s
(audio energy {candidate['audio_score']:.2f}/1, motion {candidate['motion_score']:.2f}/1).

Transcript around this segment (with [second] markers):
{transcript_window or '(no transcript available — judge on the energy stats alone)'}

Reply with ONLY this JSON object:
{{
 "keep": true|false,
 "reason": "one sentence",
 "start": <number, adjusted start second — begin at a natural sentence/beat>,
 "end": <number, adjusted end second — end on a payoff, 15-60s after start>,
 "title": "punchy YouTube Shorts title, <=90 chars, ALWAYS in English even if the source is in another language",
 "description": "2-3 sentence description with a hook, in English",
 "hashtags": ["#tag1", "#tag2", "#tag3", "#shorts"]
}}"""
    try:
        data = _json_loads_loose(_chat(prompt, json_mode=True))
    except (RuntimeError, ValueError, requests.RequestException):
        # heuristic-only degradation
        return {"keep": True, "reason": "kept by heuristic score (no LLM available)",
                "start": candidate["start"], "end": candidate["end"],
                "title": f"High-energy moment at {int(candidate['start'])}s",
                "description": "Auto-selected high-energy segment.",
                "hashtags": ["#shorts", "#clip", "#viral"]}
    # sanity-clamp the LLM's timestamps to stay near the heuristic window
    s = float(data.get("start", candidate["start"]))
    e = float(data.get("end", candidate["end"]))
    if not (candidate["start"] - 20 <= s <= candidate["end"] + 20):
        s = candidate["start"]
    if not (s + 5 <= e <= s + 90):
        e = candidate["end"] if candidate["end"] > s + 5 else s + config.SEGMENT_MIN_SEC
    if e - s < config.MIN_OUTPUT_SECONDS:  # enforce minimum clip length
        e = s + config.MIN_OUTPUT_SECONDS
    data["start"], data["end"] = s, e
    data.setdefault("keep", True)
    data.setdefault("hashtags", ["#shorts"])
    return data



_VO_STYLES = [
    "shocked disbelief, like you can't believe what you just saw",
    "a teasing question that dares the viewer to keep watching",
    "confident trash-talk, like you predicted this moment",
    "an urgent whisper that builds tension",
    "over-the-top sports-caster energy",
    "deadpan understatement that makes the moment funnier",
]
_VO_FALLBACKS = [
    "No way. Watch what happens next.",
    "You're not ready for this part.",
    "This is the moment everyone keeps replaying.",
    "Wait for it... trust me.",
    "I've watched this ten times and it still gets me.",
    "Nobody saw this coming.",
]
_VO_BANNED_OPENERS = ("and here we go", "here we go", "get ready", "and they're off",
                      "it's go time", "ladies and gentlemen", "welcome")




def _strip_preamble(text: str) -> str:
    """Remove meta-preambles a small model prepends despite instructions
    ('Here's a possible commentary:', 'Sure! ...'), and unwrap surrounding
    quotes — otherwise the narration gets read aloud with them."""
    text = text.strip()
    # drop a leading 'blah blah:' lead-in when followed by real content
    m = re.match(r"^\s*(?:sure[,!.]?\s*)?(?:here(?:'s| is)[^:]{0,60}:|"
                 r"okay[,!.]?\s*here[^:]{0,60}:|commentary:|narration:)\s*(.+)$",
                 text, re.I | re.S)
    if m and len(m.group(1).split()) >= 5:
        text = m.group(1).strip()
    # unwrap a fully-quoted body
    if len(text) >= 2 and text[0] in "\"“" and text[-1] in "\"”":
        text = text[1:-1].strip()
    return text


def commentary_script(title: str, transcript: str, seconds: float) -> str:
    """A full English commentary track narrating what happens in a clip — built
    for foreign-language / no-subtitle sources (e.g. Bilibili). Length is sized
    to the clip so spoken narration fills it without overrunning."""
    words = max(12, min(160, int(seconds / 60 * 150 * 0.85)))  # ~150 wpm, 85% fill
    have_ctx = bool(transcript and transcript.strip())
    prompt = (
        f"You are an English-speaking commentator narrating a short vertical video "
        f"for viewers who don't understand the original audio. Write a natural "
        f"spoken commentary of about {words} words (max {words + 15}) that walks the "
        f"viewer through what's happening and why it's worth watching. Conversational, "
        f"energetic but not shouty. No timestamps, no emojis, no hashtags, no stage "
        f"directions, and NO preamble — do not write 'Here's a commentary' or wrap "
        f"it in quotes. Start immediately with the first spoken word.\n"
        f"Clip title: {title}\n"
        + (f"What is said/happens (rough English translation):\n{transcript[:1200]}"
           if have_ctx else
           "No transcript available — narrate the energy and action generically, "
           "staying vague about specifics you cannot know.")
    )
    try:
        out = _chat(prompt, json_mode=False, temperature=0.8).strip()
        out = _strip_preamble(out)
        out = " ".join(out.split())  # flatten stray newlines
        if out:
            w = out.split()
            if len(w) > words + 25:  # trim to a sentence boundary near target
                out = " ".join(w[: words + 10])
                cut = out.rfind(".")
                if cut > 20:
                    out = out[: cut + 1]
            return out
    except (RuntimeError, requests.RequestException):
        pass
    return (f"Here's {title}. Watch what happens next — this clip is packed with "
            f"action you won't want to miss.")


def reaction_line(title: str, transcript: str, part: int = 1,
                  avoid: list[str] | None = None) -> str:
    """One short spoken reaction line for a clip-intro voiceover. Each part gets
    a different delivery style, and openers already used in this job (or on the
    generic-hype ban list) are rejected so a clip series doesn't sound canned."""
    avoid = avoid or []
    style = _VO_STYLES[(part - 1) % len(_VO_STYLES)]
    banned = list(_VO_BANNED_OPENERS) + [" ".join(a.lower().split()[:3]) for a in avoid]

    def bad(line: str) -> bool:
        low = line.lower()
        return any(low.startswith(b) for b in banned if b)

    prompt = (
        "Write ONE spoken reaction line (max 12 words) for the start of a video Short.\n"
        f"Delivery style: {style}.\n"
        f"It must react to something SPECIFIC from this clip — title: \"{title}\""
        + (f", what happens: {transcript[:400]}" if transcript else "")
        + ".\nNever open with generic hype like 'here we go' or 'get ready'"
        + (", and never open with: " + "; ".join(f'"{a}"' for a in avoid[:6]) if avoid else "")
        + ". No emojis, no hashtags, no quotes — reply with only the spoken words."
    )
    for attempt in range(2):
        try:
            out = _chat(prompt, json_mode=False, temperature=0.95).strip() \
                .strip('"').splitlines()[0].strip().strip('"')
            words = out.split()
            if 2 <= len(words) <= 18 and not bad(out):
                return " ".join(words[:14])
            prompt += "\nYour previous answer was too generic — be more specific and different."
        except (RuntimeError, requests.RequestException):
            break
    return _VO_FALLBACKS[(part - 1) % len(_VO_FALLBACKS)]

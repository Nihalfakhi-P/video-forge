"""FFmpeg helpers: probing, cutting, 9:16 conversion, voiceover mixing."""
import re
import subprocess
from pathlib import Path

import numpy as np

from . import config


def ffmpeg_exe() -> str:
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def _run(args: list[str]) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        [ffmpeg_exe(), "-hide_banner", "-y", *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {' '.join(args[:8])}...\n{proc.stderr[-2000:]}")
    return proc


def probe_duration(path: str | Path) -> float:
    """imageio-ffmpeg ships no ffprobe; parse duration from `ffmpeg -i` stderr."""
    proc = subprocess.run(
        [ffmpeg_exe(), "-hide_banner", "-i", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", proc.stderr)
    if not m:
        raise RuntimeError(f"Could not read duration of {path}")
    h, mnt, s = m.groups()
    return int(h) * 3600 + int(mnt) * 60 + float(s)


def probe_dimensions(path: str | Path) -> tuple[int, int]:
    """(width, height) of the first video stream, parsed from `ffmpeg -i` stderr."""
    proc = subprocess.run(
        [ffmpeg_exe(), "-hide_banner", "-i", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    m = re.search(r"Video:.*?\s(\d{2,5})x(\d{2,5})", proc.stderr)
    if not m:
        return (16, 9)
    return int(m.group(1)), int(m.group(2))


def make_title_overlay(title: str, out_png: str | Path, band_h: int,
                       w: int | None = None, h: int | None = None) -> Path:
    """Transparent PNG (full frame size) with the title as bold outlined text,
    word-wrapped and centered inside the top band [0, band_h]."""
    from PIL import Image, ImageDraw, ImageFont

    from . import config
    w = w or config.SHORT_W
    h = h or config.SHORT_H
    # strip emoji / non-BMP chars the font can't draw
    title = "".join(c for c in title if ord(c) < 0xFFFF and (c.isprintable())).strip()
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    size = 58
    font = None
    lines, line_h = [title], int(size * 1.22)
    while size >= 34:
        for name in ("arialbd.ttf", "arial.ttf"):
            try:
                font = ImageFont.truetype(name, size)
                break
            except OSError:
                continue
        if font is None:
            font = ImageFont.load_default()
            break
        # wrap to the band width
        max_w = int(w * 0.88)
        words, lines, cur = title.split(), [], ""
        for wd in words:
            trial = (cur + " " + wd).strip()
            if d.textlength(trial, font=font) <= max_w:
                cur = trial
            else:
                if cur:
                    lines.append(cur)
                cur = wd
        if cur:
            lines.append(cur)
        line_h = int(size * 1.22)
        if len(lines) * line_h <= band_h * 0.8 and len(lines) <= 3:
            break
        size -= 6
    y0 = max(20, (band_h - len(lines) * line_h) // 2)
    for i, ln in enumerate(lines):
        d.text((w // 2, y0 + i * line_h + line_h // 2), ln,
               font=font, fill=(255, 255, 255, 255), anchor="mm",
               stroke_width=4, stroke_fill=(0, 0, 0, 230))
    img.save(out_png)
    return Path(out_png)


def cut_vertical_clip(src: str | Path, start: float, end: float, out: str | Path,
                      blur_pad: bool = True, title: str = "") -> Path:
    """Cut [start, end] and convert to 9:16. blur_pad keeps the full frame over a
    blurred, zoomed copy of itself; otherwise center-crop. When a title is given
    it is burned into the top padding band above the main frame."""
    import tempfile

    w, h = config.SHORT_W, config.SHORT_H
    if blur_pad:
        vf = (
            f"split=2[bg][fg];"
            f"[bg]scale={w}:{h}:force_original_aspect_ratio=increase,"
            f"crop={w}:{h},gblur=sigma=24[bgb];"
            f"[fg]scale={w}:{h}:force_original_aspect_ratio=decrease:flags=lanczos[fgs];"
            f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2[comp]"
        )
    else:
        vf = (f"scale={w}:{h}:force_original_aspect_ratio=increase:flags=lanczos,"
              f"crop={w}:{h}[comp]")

    title_png = None
    extra_inputs: list[str] = []
    if title.strip():
        sw, sh = probe_dimensions(src)
        fg_h = min(h, int(w * sh / max(sw, 1)))
        band = max(150, (h - fg_h) // 2)  # top padding band (fallback if portrait src)
        title_png = Path(tempfile.gettempdir()) / f"title_{abs(hash((str(out), title)))}.png"
        make_title_overlay(title, title_png, band_h=band, w=w, h=h)
        extra_inputs = ["-i", str(title_png)]
        vf += ";[comp][1:v]overlay=0:0[outv]"
        last = "[outv]"
    else:
        last = "[comp]"

    base = [
        "-ss", f"{start:.2f}", "-to", f"{end:.2f}", "-i", str(src), *extra_inputs,
        "-filter_complex", vf, "-map", last, "-map", "0:a?",
        "-c:v", "libx264", "-preset", config.X264_PRESET, "-crf", str(config.CRF),
        "-profile:v", "high", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart",
        str(out),
    ]
    try:
        try:  # GPU decode of the source when the codec supports it
            _run(["-hwaccel", "d3d11va", *base])
        except RuntimeError:
            _run(base)
    finally:
        if title_png:
            title_png.unlink(missing_ok=True)
    return Path(out)


def mix_commentary(clip: str | Path, vo: str | Path, out: str | Path,
                   delay_ms: int = 300) -> Path:
    """Lay a full-length commentary track over a clip: the original audio ducks
    to 18% for the WHOLE clip (so foreign/no-subtitle audio stays as ambience
    while the English narration leads), video stream copied."""
    fc = (
        f"[0:a]volume=0.18[bg];"
        f"[1:a]adelay={delay_ms}|{delay_ms},volume=1.7[vo];"
        f"[bg][vo]amix=inputs=2:duration=first:normalize=0[aout]"
    )
    _run(["-i", str(clip), "-i", str(vo), "-filter_complex", fc,
          "-map", "0:v", "-map", "[aout]", "-c:v", "copy",
          "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
          "-movflags", "+faststart", str(out)])
    return Path(out)


def mix_reaction_voiceover(clip: str | Path, vo: str | Path, out: str | Path,
                           delay_ms: int = 400) -> Path:
    """Overlay a spoken reaction on a finished clip: original audio ducks to 25%
    while the voiceover plays, then returns. Video stream is copied (no re-encode)."""
    vo_len = probe_duration(vo)
    end = delay_ms / 1000 + vo_len + 0.3
    fc = (
        f"[0:a]volume=enable='between(t,{delay_ms / 1000 - 0.15:.2f},{end:.2f})':volume=0.25[duck];"
        f"[1:a]adelay={delay_ms}|{delay_ms},volume=1.9[vo];"
        f"[duck][vo]amix=inputs=2:duration=first:normalize=0[aout]"
    )
    _run(["-i", str(clip), "-i", str(vo), "-filter_complex", fc,
          "-map", "0:v", "-map", "[aout]", "-c:v", "copy",
          "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
          "-movflags", "+faststart", str(out)])
    return Path(out)

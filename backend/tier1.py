"""Tier 1 pre-filter: cheap signal processing to find high-energy segments.

Audio: windowed RMS energy (numpy — same math as librosa.feature.rms; librosa's
numba dependency does not support Python 3.14 yet).
Video: Farneback dense optical flow magnitude on downscaled, sparsely sampled frames.
The two normalized signals are fused into a per-second "energy" curve, and the
best non-overlapping windows become candidate segments for the LLM (Tier 2).
"""
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from . import config
from .media_utils import probe_duration


@dataclass
class Candidate:
    start: float
    end: float
    score: float
    audio_score: float
    motion_score: float
    transcript: str = ""
    # Filled in by Tier 2:
    verdict: str = "pending"
    title: str = ""
    description: str = ""
    hashtags: list[str] = field(default_factory=list)
    reason: str = ""


def audio_energy_per_second(path: Path, duration: float, progress=None) -> np.ndarray:
    """RMS energy resampled to a 1 Hz curve.

    Streams the decode one second at a time: memory stays constant no matter how
    long the video is. (Loading a full 12-hour walkthrough as float32 needed
    2.6 GB in one array and OOM'd the machine.)"""
    import subprocess
    from .media_utils import ffmpeg_exe

    sr = config.AUDIO_SR
    secs = int(np.ceil(duration))
    per_sec = np.zeros(secs)
    hop = int(sr * config.RMS_FRAME_SEC)
    bytes_per_sec = sr * 2  # s16le mono
    proc = subprocess.Popen(
        [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-i", str(path),
         "-vn", "-ac", "1", "-ar", str(sr), "-f", "s16le", "pipe:1"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    s = 0
    try:
        while s < secs:
            data = proc.stdout.read(bytes_per_sec)
            if not data:
                break
            y = np.frombuffer(data, np.int16).astype(np.float32) / 32768.0
            n = y.size // hop
            if n:
                rms = np.sqrt(np.mean(y[: n * hop].reshape(n, hop) ** 2, axis=1))
                per_sec[s] = rms.max()
            elif y.size:
                per_sec[s] = float(np.sqrt(np.mean(y ** 2)))
            s += 1
    finally:
        proc.stdout.close()
        proc.wait()
    return per_sec


def _motion_chunk(path: str, t_start: float, t_end: float, secs: int,
                  done_secs, lock) -> tuple[np.ndarray, np.ndarray]:
    """Optical-flow scan of one time slice.

    Decoding goes through an ffmpeg rawvideo pipe (multithreaded C decode,
    emits only MOTION_SAMPLE_FPS grayscale frames at analysis resolution) —
    an order of magnitude faster than a python cv2.grab() loop on long videos.
    """
    import subprocess
    from .media_utils import ffmpeg_exe

    per_sec = np.zeros(secs)
    counts = np.zeros(secs)
    w, h = config.MOTION_DOWNSCALE_W, 90
    fps = config.MOTION_SAMPLE_FPS
    frame_bytes = w * h

    def scan(hwaccel: bool) -> int:
        nonlocal per_sec, counts
        args = [ffmpeg_exe(), "-hide_banner", "-loglevel", "error"]
        if hwaccel:
            args += ["-hwaccel", "d3d11va"]  # GPU decode (NVDEC/QuickSync via D3D11)
        args += ["-ss", f"{t_start:.2f}", "-t", f"{t_end - t_start:.2f}", "-i", path,
                 "-vf", f"fps={fps},scale={w}:{h}", "-f", "rawvideo", "-pix_fmt", "gray",
                 "pipe:1"]
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        prev, n = None, 0
        try:
            while True:
                buf = proc.stdout.read(frame_bytes)
                if not buf or len(buf) < frame_bytes:
                    break
                gray = np.frombuffer(buf, np.uint8).reshape(h, w)
                t = t_start + n / fps
                n += 1
                if prev is not None:
                    flow = cv2.calcOpticalFlowFarneback(
                        prev, gray, None, 0.5, 2, 15, 2, 5, 1.1, 0)
                    mag = float(np.linalg.norm(flow, axis=2).mean())
                    s = min(secs - 1, int(t))
                    per_sec[s] += mag
                    counts[s] += 1
                prev = gray
                if n % int(fps * 5) == 0:  # every ~5s of video
                    with lock:
                        done_secs[0] += 5
        finally:
            proc.stdout.close()
            proc.wait()
        return n

    if scan(hwaccel=True) == 0:  # GPU decode unavailable for this codec -> CPU
        per_sec[:] = 0
        counts[:] = 0
        scan(hwaccel=False)
    return per_sec, counts


def motion_energy_per_second(path: Path, duration: float, progress=None,
                             workers: int = 4) -> np.ndarray:
    """Mean dense optical-flow magnitude per second. The video is split into
    time slices scanned in parallel (one VideoCapture per worker)."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    secs = int(np.ceil(duration))
    workers = max(1, min(workers, int(duration // 30) or 1))  # short videos: 1 worker
    bounds = np.linspace(0, duration, workers + 1)
    done_secs, lock = [0], threading.Lock()
    stop = threading.Event()

    def report():
        while not stop.wait(2.0):
            if progress and secs:
                progress(min(0.99, done_secs[0] / secs))

    reporter = threading.Thread(target=report, daemon=True)
    reporter.start()
    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = [ex.submit(_motion_chunk, str(path), float(bounds[i]),
                                 float(bounds[i + 1]), secs, done_secs, lock)
                       for i in range(workers)]
            results = [f.result() for f in futures]
    finally:
        stop.set()
    per_sec = sum(r[0] for r in results)
    counts = sum(r[1] for r in results)
    counts[counts == 0] = 1
    return per_sec / counts


def _gray_frames(args_input: list[str], w: int = 96, h: int = 54):
    """Yield small grayscale frames from an ffmpeg rawvideo pipe."""
    import subprocess
    from .media_utils import ffmpeg_exe
    args = [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", *args_input,
            "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"]
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        while True:
            buf = proc.stdout.read(w * h)
            if not buf or len(buf) < w * h:
                break
            yield np.frombuffer(buf, np.uint8).reshape(h, w).astype(np.float32)
    finally:
        proc.stdout.close()
        proc.wait()


def thumbnail_banned_seconds(path: Path, thumb: str, duration: float) -> np.ndarray | None:
    """Boolean mask of seconds whose frame matches the video's thumbnail image —
    creators often paste their clickbait card into the video itself; those
    frames must never end up inside a Short."""
    if not thumb or not Path(thumb).exists():
        return None
    ref = next(iter(_gray_frames(["-i", thumb, "-vf", "scale=96:54", "-frames:v", "1"])), None)
    if ref is None:
        return None
    secs = int(np.ceil(duration))
    banned = np.zeros(secs, dtype=bool)
    for i, frame in enumerate(_gray_frames(["-i", str(path), "-vf", "fps=1,scale=96:54"])):
        if i >= secs:
            break
        if np.abs(frame - ref).mean() < 16:  # near-identical to the thumbnail
            banned[i] = True
    return banned if banned.any() else None


def _normalize(x: np.ndarray) -> np.ndarray:
    if x.size == 0 or x.max() <= x.min():
        return np.zeros_like(x)
    return (x - x.min()) / (x.max() - x.min())


def find_candidates(path: Path, progress=None,
                    banned: np.ndarray | None = None) -> tuple[list[Candidate], float]:
    from concurrent.futures import ThreadPoolExecutor

    duration = probe_duration(path)
    if progress:
        progress(0.05, "analyzing audio (RMS) + motion (optical flow) in parallel")
    with ThreadPoolExecutor(max_workers=2) as ex:
        fa = ex.submit(audio_energy_per_second, path, duration)
        fm = ex.submit(
            motion_energy_per_second, path, duration,
            lambda p: progress(0.05 + p * 0.7,
                               "analyzing audio + motion in parallel (4 workers)") if progress else None)
        a = _normalize(fa.result())
        m = _normalize(fm.result())
    n = min(len(a), len(m))
    energy = 0.55 * a[:n] + 0.45 * m[:n]
    if banned is not None:  # thumbnail/title-card seconds can never be picked
        energy[banned[:n]] = -1.0
    # light smoothing so single-second spikes don't dominate
    if n >= 5:
        kernel = np.ones(5) / 5
        energy = np.convolve(energy, kernel, mode="same")

    if progress:
        progress(0.8, "selecting candidate segments")
    win = min(config.SEGMENT_MAX_SEC, max(config.SEGMENT_MIN_SEC, n // 4 or config.SEGMENT_MIN_SEC))
    win = min(win, max(5, n - 1))
    scores = np.convolve(energy, np.ones(win) / win, mode="valid")
    order = np.argsort(scores)[::-1]
    picked: list[Candidate] = []
    for start in order:
        s, e = float(start), float(start + win)
        if any(not (e <= c.start or s >= c.end) for c in picked):
            continue  # overlap
        if banned is not None and banned[int(s):int(e)].any():
            continue  # window would show the embedded thumbnail card
        seg_a = float(a[int(s):int(e)].mean()) if int(e) > int(s) else 0.0
        seg_m = float(m[int(s):int(e)].mean()) if int(e) > int(s) else 0.0
        picked.append(Candidate(start=s, end=e, score=float(scores[start]),
                                audio_score=seg_a, motion_score=seg_m))
        if len(picked) >= config.MAX_CANDIDATES:
            break
    picked.sort(key=lambda c: c.start)
    return picked, duration

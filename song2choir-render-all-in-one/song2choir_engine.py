"""Song2Choir Pro audio rendering engine.

This engine turns an uploaded song into a choir-style render by combining:
- harmonic/percussive source separation
- automatic tempo/key analysis
- generated harmony layers
- stereo choir spreading
- hall/cathedral-style reverb
- mastering-safe limiting

It is intentionally dependency-light enough to run on a Render worker/server.
"""
from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
import math
import os
import tempfile
from typing import Dict, Iterable, List, Tuple

import librosa
import numpy as np
import soundfile as sf
from scipy import signal, ndimage

TARGET_SR = 44100
MAX_SECONDS_DEFAULT = 180


@dataclass(frozen=True)
class RenderOptions:
    style: str = "gospel"
    intensity: float = 0.72
    room: float = 0.62
    warmth: float = 0.58
    harmony: str = "gospel_stack"
    keep_original: float = 0.28


STYLE_PRESETS: Dict[str, Dict[str, float | str | List[float]]] = {
    "gospel": {
        "label": "Gospel Hall",
        "wet": 0.38,
        "tail": 2.6,
        "pre_delay": 0.035,
        "lowpass": 10750,
        "highpass": 70,
        "body": 1.05,
        "air": 0.7,
        "spread": 0.84,
    },
    "cathedral": {
        "label": "Cathedral Choir",
        "wet": 0.50,
        "tail": 4.4,
        "pre_delay": 0.060,
        "lowpass": 9200,
        "highpass": 95,
        "body": 0.94,
        "air": 0.55,
        "spread": 0.95,
    },
    "cinematic": {
        "label": "Cinematic Choir",
        "wet": 0.43,
        "tail": 3.5,
        "pre_delay": 0.045,
        "lowpass": 9800,
        "highpass": 60,
        "body": 1.12,
        "air": 0.85,
        "spread": 0.92,
    },
    "acapella": {
        "label": "Acapella Choir",
        "wet": 0.25,
        "tail": 1.55,
        "pre_delay": 0.020,
        "lowpass": 12000,
        "highpass": 85,
        "body": 1.0,
        "air": 0.9,
        "spread": 0.75,
    },
    "afrogospel": {
        "label": "Afro-Gospel Choir",
        "wet": 0.34,
        "tail": 2.15,
        "pre_delay": 0.030,
        "lowpass": 11000,
        "highpass": 65,
        "body": 1.08,
        "air": 0.82,
        "spread": 0.82,
    },
}

HARMONY_PRESETS: Dict[str, List[Tuple[float, float, float]]] = {
    # semitone, cents, gain
    "unison": [(0, -14, 0.55), (0, 0, 0.75), (0, 13, 0.55), (12, 4, 0.18), (-12, -3, 0.16)],
    "soft_triad": [(0, 0, 0.62), (4, -5, 0.31), (7, 6, 0.30), (-5, -4, 0.22), (12, 3, 0.12)],
    "gospel_stack": [(0, 0, 0.60), (3, -6, 0.22), (4, 5, 0.30), (7, -3, 0.28), (10, 6, 0.18), (-5, -7, 0.19), (-12, 2, 0.16)],
    "cinematic_wide": [(-12, -4, 0.22), (-5, 3, 0.30), (0, -9, 0.44), (0, 8, 0.44), (7, -5, 0.28), (12, 4, 0.18)],
    "cathedral_open": [(-12, 0, 0.18), (-7, -4, 0.22), (0, -8, 0.42), (0, 7, 0.42), (5, 3, 0.24), (12, -3, 0.14)],
}

NOTE_NAMES = ["C", "C#", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B"]


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _safe_normalize(y: np.ndarray, peak: float = 0.93) -> np.ndarray:
    y = np.asarray(y, dtype=np.float32)
    max_amp = float(np.max(np.abs(y))) if y.size else 0.0
    if max_amp < 1e-8:
        return y
    return (y / max_amp * peak).astype(np.float32)


def _butter_filter(y: np.ndarray, sr: int, cutoff: float, btype: str, order: int = 3) -> np.ndarray:
    nyq = sr / 2.0
    cutoff = clamp(cutoff, 20.0, nyq - 100.0)
    sos = signal.butter(order, cutoff / nyq, btype=btype, output="sos")
    return signal.sosfiltfilt(sos, y).astype(np.float32)


def _band_emphasis(y: np.ndarray, sr: int, warmth: float, body: float, air: float) -> np.ndarray:
    """A simple choir-like tone shaper: warm low mids, softened harsh highs, controlled air."""
    warmth = clamp(warmth, 0, 1)
    low_mid = _butter_filter(y, sr, 900 + 450 * warmth, "lowpass", order=2)
    bright = y - _butter_filter(y, sr, 2500, "lowpass", order=2)
    body_gain = 0.36 + 0.32 * warmth
    air_gain = 0.10 + 0.25 * air * (1.0 - warmth * 0.25)
    shaped = y * 0.72 + low_mid * body_gain * body + bright * air_gain
    return shaped.astype(np.float32)


def _soft_compress(y: np.ndarray, drive: float = 1.4) -> np.ndarray:
    # Smooth tape-like saturation; safer than hard clipping.
    y = np.asarray(y, dtype=np.float32)
    return np.tanh(y * drive) / np.tanh(drive)


def _delay(y: np.ndarray, samples: int) -> np.ndarray:
    if samples <= 0:
        return y
    out = np.zeros_like(y)
    out[samples:] = y[:-samples]
    return out


def _pan_mono(y: np.ndarray, pan: float) -> np.ndarray:
    """Constant-power pan: pan -1 left, +1 right."""
    pan = clamp(pan, -1, 1)
    angle = (pan + 1) * math.pi / 4
    left = math.cos(angle) * y
    right = math.sin(angle) * y
    return np.stack([left, right], axis=0).astype(np.float32)


def _spectral_pitch_shift(y: np.ndarray, sr: int, n_steps: float) -> np.ndarray:
    """Fast choir-pad pitch shift approximation.

    This shifts STFT frequency bins instead of doing a full phase-vocoder pitch shift.
    It is much faster for cloud renders and works well for blended choir layers.
    """
    if abs(n_steps) < 0.001:
        return y.copy()
    factor = 2 ** (n_steps / 12.0)
    n_fft = 2048
    hop = 512
    S = librosa.stft(y, n_fft=n_fft, hop_length=hop)
    real = ndimage.zoom(S.real, (factor, 1.0), order=1)
    imag = ndimage.zoom(S.imag, (factor, 1.0), order=1)
    shifted = real + 1j * imag
    out = np.zeros_like(S)
    bins = min(out.shape[0], shifted.shape[0])
    out[:bins, :] = shifted[:bins, :]
    y_out = librosa.istft(out, hop_length=hop, length=len(y))
    return np.nan_to_num(y_out.astype(np.float32), nan=0.0, posinf=0.0, neginf=0.0)


def _choir_layer(y: np.ndarray, sr: int, semitone: float, cents: float, delay_ms: float, pan: float, gain: float) -> np.ndarray:
    shift_steps = semitone + cents / 100.0
    shifted = _spectral_pitch_shift(y, sr, shift_steps)
    # Human-ish micro timing variation.
    shifted = _delay(shifted.astype(np.float32), int(sr * delay_ms / 1000.0))
    return _pan_mono(shifted * gain, pan)


def _make_reverb_ir(sr: int, tail_seconds: float, wet: float, room: float) -> np.ndarray:
    tail_seconds = clamp(tail_seconds, 0.35, 6.0)
    n = int(sr * tail_seconds)
    rng = np.random.default_rng(4242)
    t = np.linspace(0, tail_seconds, n, endpoint=False)
    decay = np.exp(-t * (2.6 - clamp(room, 0, 1) * 1.25))
    noise = rng.normal(0, 1, n).astype(np.float32)
    ir = noise * decay
    # Early reflections for hall/choir space.
    for ms, amp in [(19, 0.55), (37, 0.34), (61, 0.24), (89, 0.16), (127, 0.10)]:
        idx = int(sr * ms / 1000.0)
        if idx < n:
            ir[idx] += amp
    ir = _butter_filter(ir, sr, 10500, "lowpass", order=2)
    ir = _safe_normalize(ir, peak=wet)
    return ir.astype(np.float32)


def _apply_reverb(stereo: np.ndarray, sr: int, tail: float, wet: float, pre_delay: float, room: float) -> np.ndarray:
    ir = _make_reverb_ir(sr, tail, wet, room)
    pre = int(sr * pre_delay)
    if pre > 0:
        ir = np.concatenate([np.zeros(pre, dtype=np.float32), ir])
    left = signal.fftconvolve(stereo[0], ir, mode="full")[: stereo.shape[1]]
    right = signal.fftconvolve(stereo[1], ir[::-1].copy(), mode="full")[: stereo.shape[1]]
    wet_sig = np.stack([left, right], axis=0).astype(np.float32)
    return stereo * (1.0 - wet) + wet_sig


def _estimate_key(y: np.ndarray, sr: int) -> str:
    try:
        chroma = librosa.feature.chroma_cqt(y=y, sr=sr)
        profile = np.mean(chroma, axis=1)
        if not np.isfinite(profile).all() or np.sum(profile) <= 0:
            return "Unknown"
        root = int(np.argmax(profile))
        return NOTE_NAMES[root]
    except Exception:
        return "Unknown"


def _estimate_tempo(y: np.ndarray, sr: int) -> int:
    try:
        tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
        if isinstance(tempo, np.ndarray):
            tempo = tempo.item()
        if not np.isfinite(tempo):
            return 0
        return int(round(float(tempo)))
    except Exception:
        return 0


def load_audio(path: str, max_seconds: int | None = None) -> Tuple[np.ndarray, int]:
    max_seconds = max_seconds or int(os.getenv("MAX_AUDIO_SECONDS", MAX_SECONDS_DEFAULT))
    duration = librosa.get_duration(path=path)
    if duration > max_seconds:
        duration = max_seconds
    y, sr = librosa.load(path, sr=TARGET_SR, mono=True, duration=duration)
    y = np.nan_to_num(y.astype(np.float32), nan=0.0, posinf=0.0, neginf=0.0)
    return _safe_normalize(y, 0.88), sr


def render_choir(path: str, options: RenderOptions) -> Tuple[bytes, Dict[str, str | int | float]]:
    style = STYLE_PRESETS.get(options.style, STYLE_PRESETS["gospel"])
    harmony_layers = HARMONY_PRESETS.get(options.harmony, HARMONY_PRESETS["gospel_stack"])
    intensity = clamp(float(options.intensity), 0.05, 1.0)
    room = clamp(float(options.room), 0.0, 1.0)
    warmth = clamp(float(options.warmth), 0.0, 1.0)
    keep_original = clamp(float(options.keep_original), 0.0, 0.55)

    y, sr = load_audio(path)
    if len(y) < sr // 2:
        raise ValueError("Audio is too short. Upload at least 1 second of audio.")

    tempo = _estimate_tempo(y, sr)
    key = _estimate_key(y, sr)

    # Harmonic/percussive split makes the choir layer smoother and less drum-heavy.
    harmonic, percussive = librosa.effects.hpss(y, margin=(1.0, 5.0))
    harmonic = _safe_normalize(harmonic, 0.82)

    # Clean up sub and excessive top before making pitch layers.
    highpass = float(style["highpass"])
    lowpass = float(style["lowpass"])
    harmonic = _butter_filter(harmonic, sr, highpass, "highpass", order=2)
    harmonic = _butter_filter(harmonic, sr, lowpass, "lowpass", order=3)
    harmonic = _band_emphasis(harmonic, sr, warmth, float(style["body"]), float(style["air"]))

    length = len(harmonic)
    choir = np.zeros((2, length), dtype=np.float32)
    pan_positions = np.linspace(-float(style["spread"]), float(style["spread"]), len(harmony_layers))

    # Build choir stack with slight timing differences and gains.
    for idx, (semi, cents, gain) in enumerate(harmony_layers):
        delay_ms = 11 + idx * (13 + 8 * room)
        pan = float(pan_positions[idx])
        layer_gain = gain * (0.55 + 0.95 * intensity)
        layer = _choir_layer(harmonic, sr, semi, cents, delay_ms, pan, layer_gain)
        choir[:, : layer.shape[1]] += layer[:, :length]

    choir = _safe_normalize(choir, 0.78)

    # Add a low octave body very gently for cinematic/fullness.
    if options.style in {"cinematic", "cathedral", "gospel", "afrogospel"}:
        try:
            low_oct = _spectral_pitch_shift(harmonic, sr, -12)
            low_oct = _butter_filter(low_oct, sr, 1700, "lowpass", order=2) * (0.10 + 0.13 * intensity)
            choir += _pan_mono(low_oct[:length], 0.0)
        except Exception:
            pass

    # Optional original bed for musicality; acapella keeps less of it.
    original_stereo = np.stack([y, y], axis=0)
    if options.style == "acapella":
        original_amount = min(keep_original, 0.16)
    else:
        original_amount = keep_original
    # Keep only a little percussive texture for afro-gospel/gospel if requested.
    perc_amount = 0.055 if options.style in {"gospel", "afrogospel"} else 0.02
    bed = original_stereo * original_amount + np.stack([percussive, percussive], axis=0) * perc_amount
    mix = choir * (0.72 + 0.30 * intensity) + bed

    # Reverb and master.
    wet = clamp(float(style["wet"]) * (0.55 + 0.85 * room), 0.12, 0.65)
    tail = float(style["tail"]) * (0.75 + 0.65 * room)
    pre_delay = float(style["pre_delay"])
    mix = _apply_reverb(mix, sr, tail, wet, pre_delay, room)
    mix = _butter_filter(mix, sr, 35, "highpass", order=2)
    mix = _soft_compress(mix, drive=1.10 + 0.55 * intensity)
    mix = _safe_normalize(mix, 0.92)

    # soundfile expects shape (samples, channels)
    out = mix.T.astype(np.float32)
    buffer = BytesIO()
    sf.write(buffer, out, sr, format="WAV", subtype="PCM_16")
    buffer.seek(0)

    metadata = {
        "style": str(style["label"]),
        "tempo_bpm": tempo,
        "estimated_key": key,
        "sample_rate": sr,
        "duration_seconds": round(len(y) / sr, 2),
        "engine": "Song2Choir Pro server render v1",
    }
    return buffer.read(), metadata


def render_from_upload_bytes(data: bytes, suffix: str, options: RenderOptions) -> Tuple[bytes, Dict[str, str | int | float]]:
    suffix = suffix if suffix.startswith(".") else f".{suffix}"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(data)
        tmp_path = tmp.name
    try:
        return render_choir(tmp_path, options)
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass

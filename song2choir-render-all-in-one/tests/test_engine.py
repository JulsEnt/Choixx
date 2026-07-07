from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import soundfile as sf

from song2choir_engine import RenderOptions, render_choir


def test_render_choir_smoke():
    sr = 44100
    t = np.linspace(0, 1.2, int(sr * 1.2), endpoint=False)
    y = 0.25 * np.sin(2 * np.pi * 220 * t) + 0.15 * np.sin(2 * np.pi * 330 * t)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        path = f.name
    try:
        sf.write(path, y, sr)
        out, meta = render_choir(path, RenderOptions(style="gospel", harmony="soft_triad"))
        assert len(out) > 1000
        assert meta["engine"].startswith("Song2Choir")
    finally:
        Path(path).unlink(missing_ok=True)

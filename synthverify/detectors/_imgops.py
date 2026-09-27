"""NumPy-only image operations shared by detectors (no mandatory scipy/cv2)."""

from __future__ import annotations

import numpy as np
from PIL import Image


def to_gray(img: Image.Image) -> np.ndarray:
    """RGB(A)/palette image -> float64 grayscale array in [0, 255]."""
    if img.mode not in ("L", "I", "I;16"):
        img = img.convert("RGB")
    arr = np.asarray(img.convert("L"), dtype=np.float64)
    return arr


def box_blur(arr: np.ndarray, size: int = 3) -> np.ndarray:
    """Fast separable box blur via cumsum (dependency-free uniform filter)."""
    if size % 2 == 0:
        size += 1
    r = size // 2
    a = arr.astype(np.float64)
    # horizontal
    pad = np.pad(a, ((0, 0), (r, r)), mode="edge")
    c = np.cumsum(pad, axis=1)
    a = (c[:, 2 * r :] - c[:, : -2 * r or None]) / (2 * r + 1)
    # vertical
    pad = np.pad(a, ((r, r), (0, 0)), mode="edge")
    c = np.cumsum(pad, axis=0)
    return (c[2 * r :, :] - c[: -2 * r or None, :]) / (2 * r + 1)


def high_pass(arr: np.ndarray, size: int = 3) -> np.ndarray:
    return arr - box_blur(arr, size)


def block_view(arr: np.ndarray, grid: int = 8) -> tuple[np.ndarray, int, int]:
    """Split 2-D array into a ``grid x grid`` grid of (roughly) equal blocks.

    Returns ``(blocks, bh, bw)`` where ``blocks`` has shape (grid*grid, bh, bw).
    """
    h, w = arr.shape
    bh = max(1, h // grid)
    bw = max(1, w // grid)
    blocks = []
    for i in range(grid):
        for j in range(grid):
            y0, x0 = i * bh, j * bw
            blocks.append(arr[y0 : y0 + bh, x0 : x0 + bw])
    return np.stack(blocks), bh, bw


def robust_std(values: np.ndarray) -> float:
    """1.4826 * MAD - outlier-resistant standard deviation estimate."""
    med = np.median(values)
    return float(1.4826 * np.median(np.abs(values - med)))


def logistic(x: float, center: float = 0.0, scale: float = 1.0) -> float:
    """Smooth 0..1 squashing: 0.5 at ``x == center``."""
    import math

    return 1.0 / (1.0 + math.exp(-(x - center) / scale))


def region_name(grid: int, block_index: int) -> str:
    """Human-friendly region label like 'rows 3-4, cols 1-2 of an 8x8 grid'."""
    i, j = divmod(block_index, grid)
    return f"grid cell (row {i + 1}, col {j + 1} of {grid}x{grid})"

from __future__ import annotations

import cv2
import numpy as np


# Production overlay style — clean, no decorative glyphs, resolution-aware.
#   * timestamp panel ......... top-left
#   * alert info (severity / title / location) .... bottom-right
#   * subject chip + corner-bracket box ........... on the detection
#   * thin severity accent strip .................. top edge
#   * brand watermark ............................. bottom-left
_FONT = cv2.FONT_HERSHEY_DUPLEX                     # cleaner than SIMPLEX

# Severity -> accent colour (BGR).
_SEVERITY_COLORS = {
    "critical": (64, 64, 232),    # red
    "warning":  (40, 168, 248),   # amber
    "info":     (132, 196, 96),   # green
}
_DEFAULT_COLOR = (196, 156, 72)   # calm blue
_WHITE = (245, 245, 245)
_MUTED = (203, 203, 203)
_PANEL = (16, 16, 16)


def _sev_color(severity: str | None) -> tuple[int, int, int]:
    return _SEVERITY_COLORS.get((severity or "").lower(), _DEFAULT_COLOR)


def _metrics(w: int) -> dict:
    s = max(0.5, min(1.5, w / 1280.0))            # scale off a 1280px reference
    r = lambda v: max(1, int(round(v * s)))       # noqa: E731
    return {
        "title": 0.82 * s, "body": 0.58 * s, "tag": 0.5 * s,
        "thick": r(2), "thin": r(1),
        "pad": r(13), "gap": r(7), "chip_pad": r(7),
        "strip": r(4), "s": s,
    }


def _tsize(text: str, scale: float, thick: int) -> tuple[int, int, int]:
    (tw, th), base = cv2.getTextSize(text, _FONT, scale, thick)
    return tw, th, base


def _panel(img: np.ndarray, x1: int, y1: int, x2: int, y2: int,
           alpha: float = 0.5, color: tuple[int, int, int] = _PANEL) -> None:
    """Alpha-blended filled rectangle (the premium translucent look)."""
    h, w = img.shape[:2]
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(w, x2), min(h, y2)
    if x2 <= x1 or y2 <= y1:
        return
    roi = img[y1:y2, x1:x2]
    cv2.addWeighted(np.full_like(roi, color), alpha, roi, 1 - alpha, 0, roi)


def _put_top(img: np.ndarray, text: str, x: int, top: int,
             scale: float, color: tuple[int, int, int], thick: int) -> int:
    """Draw text whose TOP edge sits at `top`. Returns the line's advance (px)."""
    tw, th, base = _tsize(text, scale, thick)
    cv2.putText(img, text, (x, top + th), _FONT, scale, color, thick, cv2.LINE_AA)
    return th + base


def _corner_box(img: np.ndarray, x1: int, y1: int, x2: int, y2: int,
                color: tuple[int, int, int], thick: int) -> None:
    """L-shaped corner brackets + a faint full rectangle (security-grade look)."""
    cv2.rectangle(img, (x1, y1), (x2, y2), color, max(1, thick - 1), cv2.LINE_AA)
    L = int(max(14, 0.18 * min(x2 - x1, y2 - y1)))
    for cx, cy, dx, dy in ((x1, y1, 1, 1), (x2, y1, -1, 1),
                           (x1, y2, 1, -1), (x2, y2, -1, -1)):
        cv2.line(img, (cx, cy), (cx + dx * L, cy), color, thick, cv2.LINE_AA)
        cv2.line(img, (cx, cy), (cx, cy + dy * L), color, thick, cv2.LINE_AA)


def annotate_event_frame(
    frame: np.ndarray,
    bbox: tuple[float, float, float, float] | None,
    *,
    timestamp_text: str = "",
    title: str = "",
    location: str = "",
    subject: str | None = None,
    severity: str = "info",
    brand: str = "CERAVIS",
) -> np.ndarray:
    """
    Render a production-grade annotated snapshot on a COPY of the frame (BGR).

    Layout: timestamp top-left, alert info (severity tag / title / location)
    bottom-right, a subject chip + corner-bracket box on the detection, a thin
    severity accent strip along the top, and a subtle brand watermark. Never
    mutates the input frame.
    """
    img = frame.copy()
    h, w = img.shape[:2]
    m = _metrics(w)
    color = _sev_color(severity)

    # ---- top severity accent strip ----------------------------------
    _panel(img, 0, 0, w, m["strip"], alpha=0.85, color=color)

    # ---- detection box + subject chip -------------------------------
    if bbox is not None:
        x1, y1, x2, y2 = (int(v) for v in bbox)
        _corner_box(img, x1, y1, x2, y2, color, m["thick"] + 1)
        chip = subject or "Person"
        ctw, cth, cbase = _tsize(chip, m["body"], m["thin"] + 1)
        cw, ch = ctw + 2 * m["chip_pad"], cth + cbase + m["chip_pad"]
        cx1 = min(x1, w - cw)
        cy2 = max(ch + m["strip"], y1)              # sit just above the box
        cy1 = cy2 - ch
        cv2.rectangle(img, (cx1, cy1), (cx1 + cw, cy2), color, -1, cv2.LINE_AA)
        _put_top(img, chip, cx1 + m["chip_pad"], cy1 + m["chip_pad"] // 2,
                 m["body"], _WHITE, m["thin"] + 1)

    # ---- timestamp panel (top-left) ---------------------------------
    if timestamp_text:
        tw, th, base = _tsize(timestamp_text, m["body"], m["thin"] + 1)
        py1 = m["strip"]
        py2 = py1 + th + base + 2 * m["gap"]
        _panel(img, 0, py1, tw + 2 * m["pad"], py2, alpha=0.5)
        _put_top(img, timestamp_text, m["pad"], py1 + m["gap"],
                 m["body"], _WHITE, m["thin"] + 1)

    # ---- alert info stack (bottom-right) ----------------------------
    tag = (severity or "").upper()
    rows: list[tuple[str, float, tuple, int]] = []
    if title:
        rows.append((title, m["title"], _WHITE, m["thick"]))
    if location:
        rows.append((location, m["body"], _MUTED, m["thin"] + 1))

    if tag or rows:
        ttw, tth, tbase = _tsize(tag, m["tag"], m["thin"] + 1) if tag else (0, 0, 0)
        chip_h = (tth + tbase + m["chip_pad"]) if tag else 0
        block_w = max([ttw + 2 * m["chip_pad"]] +
                      [_tsize(t, sc, tk)[0] for t, sc, _, tk in rows] or [0])
        rows_h = sum(_tsize(t, sc, tk)[1] + _tsize(t, sc, tk)[2] + m["gap"]
                     for t, sc, _, tk in rows)
        block_h = chip_h + (m["gap"] if tag and rows else 0) + rows_h + m["pad"]

        margin = m["pad"]
        right = w - margin
        left = right - block_w - 2 * m["pad"]
        bottom = h - margin
        top = bottom - block_h
        _panel(img, left, top, right, bottom, alpha=0.55)

        y = top + m["pad"]
        if tag:                                     # filled severity chip
            chip_x2 = right - m["pad"]
            chip_x1 = chip_x2 - (ttw + 2 * m["chip_pad"])
            cv2.rectangle(img, (chip_x1, y), (chip_x2, y + chip_h),
                          color, -1, cv2.LINE_AA)
            _put_top(img, tag, chip_x1 + m["chip_pad"], y + m["chip_pad"] // 2,
                     m["tag"], _WHITE, m["thin"] + 1)
            y += chip_h + (m["gap"] if rows else 0)
        for text, scale, col, thick in rows:        # right-aligned text rows
            lw = _tsize(text, scale, thick)[0]
            y += _put_top(img, text, right - m["pad"] - lw, y, scale, col, thick)
            y += m["gap"]

    # ---- brand watermark (bottom-left) ------------------------------
    if brand:
        bw, bh, bbase = _tsize(brand, m["tag"], m["thin"])
        _put_top(img, brand, m["pad"], h - m["pad"] - bh - bbase,
                 m["tag"], _MUTED, m["thin"])
    return img

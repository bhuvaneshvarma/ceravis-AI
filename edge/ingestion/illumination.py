from __future__ import annotations

"""
Night vision — which cameras are seeing in COLOUR and which in INFRARED.

This module is the ONE owner of that fact. Every consumer (appearance
embedding, the ReID gallery, the target lock, the visitor rule, the no_motion
detector, the API) reads it from here; nothing re-derives it.

Why it matters. When a Tapo camera's light falls, it pulls its IR-cut filter,
floods the room with 850 nm LEDs and streams a MONOCHROME picture. Brightness
in that picture is infrared reflectance, not visible colour — a navy shirt can
render white — so an appearance model trained on colour sees a different
person. Detection, pose and motion are shape-driven and barely care; identity
cares a great deal. Knowing the modality per camera is what lets identity
switch to its night rules instead of silently failing.

Evidence, in order of authority:

  1. THE IMAGE. Modality is a property of the pixels, so the pixels decide.
     The measure is colour VARIATION: per-pixel chroma distance from the frame's
     own median chroma (90th percentile). A uniform cast — a white-balance tint,
     the faint magenta of a sensor without its filter — is not colour and does
     not count; a room with differently-coloured things in it does. IR frames
     sit near zero, any real colour scene well above. Outside a dead band the
     image is decisive on its own.
  2. THE CAMERA, over ONVIF (Imaging GetImagingSettings -> IrCutFilter),
     read-only and polled slowly. ON = filter in (colour), OFF = filter out
     (infrared), AUTO = "the camera decides", which says nothing about NOW. It
     only settles the dead band (a dim colour scene vs. IR). Tapo reports no
     IrCutFilter at all (bench dump, 2026-08), so on Tapo the image decides
     alone; a camera that does report it gets the tie-break for free.

A switch needs `illumination_confirm_samples` agreeing decisive samples in a
row, so a lamp flicking or a passing headlight cannot flap the mode. Each switch
bumps the camera's EPOCH — the cheap "did the modality change since I last
looked" token consumers use to drop state that belongs to the other modality
(appearance history, a pixel-motion reference).

Unknown cameras read as COLOUR — exactly the behaviour before night vision
existed — so a disabled or not-yet-sampled monitor changes nothing.
"""

import logging
import threading
import time
from dataclasses import dataclass, replace
from enum import Enum

import numpy as np

from common import clock
from config.settings import settings

try:
    import cv2
    _HAVE_CV2 = True
except ImportError:                                  # pragma: no cover
    cv2 = None                                       # type: ignore
    _HAVE_CV2 = False


logger = logging.getLogger("illumination")


class Modality(str, Enum):
    COLOR = "color"
    IR = "ir"


@dataclass(frozen=True, slots=True)
class SceneLight:
    """One camera's current modality and the evidence behind it."""
    modality: Modality
    epoch: int                     # bumps on every modality change
    since: str                     # edge-local ISO time the modality began
    chroma: float | None           # last colour-variation measurement
    luma: float | None             # last mean brightness (0..255)
    onvif_ir_cut: str | None       # ON / OFF / AUTO, or None = not reported
    decided_by: str                # "image" | "camera" | "default"

    def describe(self) -> dict:
        return {
            "modality": self.modality.value,
            "night_vision": self.modality is Modality.IR,
            "since": self.since,
            "chroma": None if self.chroma is None else round(self.chroma, 2),
            "luma": None if self.luma is None else round(self.luma, 1),
            "onvif_ir_cut": self.onvif_ir_cut,
            "decided_by": self.decided_by,
        }


# ---- measurement (pure) --------------------------------------------------

_MEASURE_WIDTH = 160       # colour variation survives heavy downscaling
# A picture must show SOMETHING to have a modality. A decoder's error-
# concealment frame (a broken stream, before the next keyframe) is flat
# neutral grey — no colour, so it read as infrared: the living room flipped to
# "night vision" 129 times in daylight on 2026-09-25 (luma 128, colour
# variation 0-2), resetting the tracker's appearance each time. Real frames on
# the bench, day and infrared: luma std 55-69 on the 160-px image, neutral mid-
# grey pixels <= 2.5%. Flat (std < 8) or mostly mid-grey (> 50%) = no opinion.
_MIN_LUMA_STD = 8.0
_MAX_MIDGREY_FRAC = 0.5


def measure(img: np.ndarray | None) -> tuple[float | None, float | None]:
    """(colour_variation, mean_luma) of a BGR image.

    colour_variation is the 90th percentile of |Cr - median Cr| + |Cb - median
    Cb| over pixels that are neither crushed nor clipped (chroma is noise there).
    Returns (None, luma) when the picture cannot be judged: too few usable
    pixels (a room so dark the sensor shows only noise), or no picture at all
    (a flat / decoder-grey frame)."""
    if img is None or img.size == 0 or not _HAVE_CV2:
        return None, None
    if img.ndim == 2 or img.shape[2] == 1:
        return 0.0, float(np.mean(img))
    h, w = img.shape[:2]
    if w > _MEASURE_WIDTH:
        img = cv2.resize(img, (_MEASURE_WIDTH, max(1, int(h * _MEASURE_WIDTH / w))),
                         interpolation=cv2.INTER_AREA)
    ycc = cv2.cvtColor(img, cv2.COLOR_BGR2YCrCb).reshape(-1, 3).astype(np.float32)
    y = ycc[:, 0]
    luma = float(np.mean(y))
    midgrey = np.abs(ycc - 128.0).max(axis=1) <= 3.0
    if float(np.std(y)) < _MIN_LUMA_STD or float(np.mean(midgrey)) > _MAX_MIDGREY_FRAC:
        return None, luma
    usable = (y > 20.0) & (y < 235.0)
    if int(np.count_nonzero(usable)) < max(16, usable.size // 20):
        return None, luma
    cr, cb = ycc[usable, 1], ycc[usable, 2]
    var = np.abs(cr - np.median(cr)) + np.abs(cb - np.median(cb))
    return float(np.percentile(var, 90.0)), luma


def monochrome(img: np.ndarray) -> np.ndarray:
    """The IR view of an image: luminance only, as 3 identical channels.

    ONE definition, used both to embed a live infrared crop and to build the IR
    gallery from colour enrollment photos, so the two sides of every night
    comparison are prepared identically. On a real IR crop this only removes
    residual chroma noise; on a colour photo it throws the colour away, which
    is the closest a daylight photo can get to what the camera sees at night."""
    if img is None or img.size == 0 or not _HAVE_CV2:
        return img
    if img.ndim == 2:
        return cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    return cv2.cvtColor(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), cv2.COLOR_GRAY2BGR)


def vote(chroma: float | None, onvif_ir_cut: str | None
         ) -> tuple[Modality | None, str]:
    """What ONE sample says, and on whose authority. None = no opinion."""
    if chroma is not None:
        if chroma <= settings.illumination_ir_max_chroma:
            return Modality.IR, "image"
        if chroma >= settings.illumination_color_min_chroma:
            return Modality.COLOR, "image"
    # Dead band, or the image cannot be judged: only a camera that states its
    # filter position can settle it. AUTO states nothing about now.
    if onvif_ir_cut == "OFF":
        return Modality.IR, "camera"
    if onvif_ir_cut == "ON":
        return Modality.COLOR, "camera"
    return None, ""


# ---- the per-camera state machine ----------------------------------------

class _Tracker:
    """Hysteresis for one camera. Not thread-safe; the registry locks."""

    __slots__ = ("state", "pending", "streak", "onvif")

    def __init__(self) -> None:
        self.state: SceneLight | None = None
        self.pending: Modality | None = None
        self.streak = 0
        self.onvif: str | None = None

    def feed(self, chroma: float | None, luma: float | None
             ) -> SceneLight | None:
        """Apply one sample. Returns the new state when the modality CHANGED."""
        v, why = vote(chroma, self.onvif)
        cur = self.state
        if cur is None:
            if v is None:
                return None                   # nothing decisive yet: stay unknown
            self.state = SceneLight(v, 1, clock.now_iso(), chroma, luma,
                                    self.onvif, why)
            return self.state
        # keep the latest numbers visible even when nothing changes
        self.state = replace(cur, chroma=chroma, luma=luma, onvif_ir_cut=self.onvif)
        if v is None or v is cur.modality:
            self.pending, self.streak = None, 0
            return None
        if v is not self.pending:
            self.pending, self.streak = v, 0
        self.streak += 1
        if self.streak < max(1, settings.illumination_confirm_samples):
            return None
        self.pending, self.streak = None, 0
        self.state = SceneLight(v, cur.epoch + 1, clock.now_iso(), chroma, luma,
                                self.onvif, why)
        return self.state


_lock = threading.RLock()
_cams: dict[str, _Tracker] = {}


def observe(camera_id: str, frame: np.ndarray | None) -> SceneLight | None:
    """Measure one frame for a camera and advance its state machine. Returns
    the new state on a modality change, else None. The monitor thread calls
    this; tests call it directly with synthetic frames."""
    chroma, luma = measure(frame)
    with _lock:
        changed = _cams.setdefault(camera_id, _Tracker()).feed(chroma, luma)
    if changed is not None:
        logger.info("%s: %s (by %s — colour variation %s, luma %s, ONVIF "
                    "IrCutFilter %s)", camera_id,
                    "NIGHT VISION (infrared)" if changed.modality is Modality.IR
                    else "colour", changed.decided_by,
                    "n/a" if chroma is None else f"{chroma:.1f}",
                    "n/a" if luma is None else f"{luma:.0f}",
                    changed.onvif_ir_cut or "not reported")
    return changed


def set_onvif_ir_cut(camera_id: str, mode: str | None) -> None:
    with _lock:
        _cams.setdefault(camera_id, _Tracker()).onvif = mode


def state(camera_id: str) -> SceneLight | None:
    with _lock:
        t = _cams.get(camera_id)
        return t.state if t is not None else None


def modality(camera_id: str) -> Modality:
    s = state(camera_id)
    return s.modality if s is not None else Modality.COLOR


def is_ir(camera_id: str) -> bool:
    return modality(camera_id) is Modality.IR


def epoch(camera_id: str) -> int:
    s = state(camera_id)
    return s.epoch if s is not None else 0


def describe(camera_id: str) -> dict:
    s = state(camera_id)
    if s is not None:
        return s.describe()
    return {"modality": Modality.COLOR.value, "night_vision": False, "since": None,
            "chroma": None, "luma": None, "onvif_ir_cut": None,
            "decided_by": "default"}


def reset() -> None:
    """Forget every camera (tests; a monitor restart)."""
    with _lock:
        _cams.clear()


# ---- the monitor ---------------------------------------------------------

class IlluminationMonitor:
    """Samples each camera's newest frame about once a second (a 160-px
    downscale — negligible CPU) and, on its own slow thread, asks each ONVIF
    camera for its IrCutFilter position. Never touches the video path."""

    def __init__(self, frame_buffer) -> None:
        self._frames = frame_buffer
        self._last_frame: dict[str, int] = {}
        self._onvif_next: dict[str, float] = {}
        self._onvif_seen: dict[str, str | None] = {}
        self._running = False
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._threads = [threading.Thread(target=self._frames_loop, daemon=True,
                                          name="illumination")]
        if settings.illumination_onvif:
            self._threads.append(threading.Thread(
                target=self._onvif_loop, daemon=True, name="illumination-onvif"))
        for t in self._threads:
            t.start()
        logger.info("Night-vision monitor started (image%s)",
                    " + ONVIF IrCutFilter" if settings.illumination_onvif else "")

    def stop(self) -> None:
        self._running = False

    def join(self, timeout: float | None = None) -> None:
        for t in self._threads:
            t.join(timeout)

    # ---- image ----------------------------------------------------------
    def _frames_loop(self) -> None:
        interval = max(0.2, settings.illumination_poll_secs)
        while self._running:
            t0 = time.perf_counter()
            try:
                for cam, fd in self._frames.get_all_latest().items():
                    if self._last_frame.get(cam) == fd.frame_id:
                        continue              # a stalled camera adds no evidence
                    self._last_frame[cam] = fd.frame_id
                    observe(cam, fd.frame)
            except Exception:
                logger.exception("illumination sample failed")
            sleep = interval - (time.perf_counter() - t0)
            if sleep > 0:
                time.sleep(sleep)

    # ---- camera (ONVIF) -------------------------------------------------
    def _onvif_loop(self) -> None:
        while self._running:
            try:
                self._poll_onvif()
            except Exception:
                logger.exception("illumination ONVIF poll failed")
            for _ in range(10):               # stop within a second
                if not self._running:
                    return
                time.sleep(1.0)

    def _poll_onvif(self) -> None:
        from configuration.camera_config import CameraConfig
        from onvif.client import OnvifCamera
        from onvif.soap import OnvifError
        now = time.monotonic()
        for cam in CameraConfig().get_enabled():
            if not cam.onvif_xaddr or now < self._onvif_next.get(cam.camera_id, 0.0):
                continue
            try:
                mode = OnvifCamera(cam.onvif_xaddr, cam.onvif_username or "",
                                   cam.onvif_password or "").ir_cut_filter()
            except OnvifError as exc:
                # Unreachable / auth: no opinion, try again at the normal cadence.
                logger.debug("%s: IrCutFilter read failed: %s", cam.camera_id, exc)
                mode, wait = None, settings.illumination_onvif_poll_secs
            else:
                # A camera that does not report the filter (Tapo) is asked
                # again only rarely — firmware updates can add it.
                wait = (settings.illumination_onvif_poll_secs if mode
                        else settings.illumination_onvif_recheck_secs)
            if (cam.camera_id not in self._onvif_seen
                    or self._onvif_seen[cam.camera_id] != mode):
                logger.info("%s: ONVIF IrCutFilter = %s", cam.camera_id,
                            mode or "not reported (the image decides)")
                self._onvif_seen[cam.camera_id] = mode
            set_onvif_ir_cut(cam.camera_id, mode)
            self._onvif_next[cam.camera_id] = now + wait

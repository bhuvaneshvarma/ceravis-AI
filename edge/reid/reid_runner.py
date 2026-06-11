from __future__ import annotations

import logging
import threading
import time

from datetime import datetime, timezone

from config.settings import settings
from ingestion.frame_buffer import FrameBuffer
from reid.faiss_index import FaissGallery
from reid.identity_buffer import IdentityBuffer
from reid.identity_schema import Identity
from reid.fastreid_extractor import FastReidExtractor
from tracking.track_buffer import TrackBuffer


logger = logging.getLogger("reid")


class ReIDRunner:
    """
    Runs OSNet on tracked person crops at settings.reid_fps (default 2 FPS).

    Strategy:
      - Only crop already-tracked persons (no wasted compute).
      - One ID per (camera_id, track_id) cached; re-query only every N frames.
    """

    REQUERY_EVERY_N_FRAMES = 10

    def __init__(
        self,
        frame_buffer: FrameBuffer,
        track_buffer: TrackBuffer,
        identity_buffer: IdentityBuffer,
        gallery: FaissGallery,
    ) -> None:
        self._frames = frame_buffer
        self._tracks = track_buffer
        self._identities = identity_buffer
        self._gallery = gallery
        self._extractor: FastReidExtractor | None = None

        self._running = False
        self._thread: threading.Thread | None = None
        self._last_query_frame: dict[tuple[str, int], int] = {}

    @property
    def is_running(self) -> bool:
        return self._running

    def start(self) -> None:
        if self._running:
            return
        try:
            self._extractor = FastReidExtractor()
        except Exception:
            logger.exception("ReIDRunner disabled (engine missing)")
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="reid-runner",
        )
        self._thread.start()

    def stop(self) -> None:
        self._running = False

    def join(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    def _run(self) -> None:
        interval = 1.0 / settings.reid_fps
        while self._running:
            t0 = time.perf_counter()
            try:
                self._tick()
            except Exception:
                logger.exception("reid tick failed")
            sleep = interval - (time.perf_counter() - t0)
            if sleep > 0:
                time.sleep(sleep)

    def _tick(self) -> None:
        if self._extractor is None:
            return
        threshold = settings.reid_match_threshold

        for camera_id, track_result in self._tracks.get_all().items():
            frame_data = self._frames.get(camera_id)
            if frame_data is None or not track_result.tracks:
                continue

            for track in track_result.tracks:
                key = (camera_id, track.track_id)
                last = self._last_query_frame.get(key, -1)
                if frame_data.frame_id - last < self.REQUERY_EVERY_N_FRAMES \
                   and self._identities.get(camera_id, track.track_id) is not None:
                    continue

                x1 = max(0, int(track.bbox.x1))
                y1 = max(0, int(track.bbox.y1))
                x2 = min(frame_data.width, int(track.bbox.x2))
                y2 = min(frame_data.height, int(track.bbox.y2))
                crop = frame_data.frame[y1:y2, x1:x2]
                if crop.size == 0:
                    continue

                emb = self._extractor.embed(crop)
                rid, score = self._gallery.search(emb)
                self._identities.update(
                    Identity(
                        track_id=track.track_id,
                        camera_id=camera_id,
                        frame_id=frame_data.frame_id,
                        timestamp=frame_data.timestamp,
                        recipient_id=rid if score >= threshold else None,
                        is_target=score >= threshold,
                        confidence=float(score),
                    )
                )
                self._last_query_frame[key] = frame_data.frame_id

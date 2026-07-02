from __future__ import annotations

"""
Local sink for cloud payloads while the switch is OFF.

Instead of POSTing to the app server, we write exactly what WOULD have been sent
into data/cloud_outbox/<date>/: a JSON record ({kind, patientId, alertType, text,
cameraNumber, images}) plus a copy of each snapshot image — self-contained, so you
can inspect locally what the DB/S3 would have received.
"""

import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path

from config.settings import settings


logger = logging.getLogger("cloud")

_EDGE_ROOT = Path(__file__).resolve().parents[1]


def _root() -> Path:
    base = settings.data_path
    base = base if base.is_absolute() else (_EDGE_ROOT / base)
    return base / "cloud_outbox"


def write(kind: str, *, patient_id, text: str, camera_number: str,
          alert_type: str | None = None, image_paths=None) -> None:
    """Persist a would-be cloud payload locally. `image_paths` are absolute
    Paths to the snapshot(s) on disk (copied into the outbox)."""
    try:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        out = _root() / day
        out.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%H%M%S_%f")

        images = []
        for src in (image_paths or []):
            try:
                dst = out / f"{stamp}_{Path(src).name}"
                shutil.copy2(src, dst)
                images.append(dst.name)
            except Exception:
                logger.exception("outbox: copy failed for %s", src)

        record = {
            "kind": kind,                            # "alert" | "snapshot"
            "patientId": patient_id,
            "alertType": alert_type,
            "text": text,
            "cameraNumber": camera_number,
            "images": images,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        (out / f"{stamp}_{kind}.json").write_text(json.dumps(record, indent=2))
        logger.info("cloud OFF -> saved %s locally (%s, %d image(s))",
                    kind, camera_number, len(images))
    except Exception:
        logger.exception("outbox: write failed")

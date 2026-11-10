#!/usr/bin/env python3
"""
Enrollment pictures in the setup wizard: deleting one (the cross on a picture).

  the picture and its label go, the others stay; a name that is not a photo of
  this recipient (or tries to leave the folder) deletes nothing;
  a picture from the CERAVIS app is refused — it is removed in the app;
  an enrolled recipient goes back to 'review' and is told to Enroll again;
  the vectors already in the gallery are not touched by a delete;
  the last picture gone leaves nothing to review.

No model files, no TensorRT. Runs on the dev box:

    python tests/test_enroll_media.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

import cv2
import numpy as np

EDGE = Path(__file__).resolve().parents[1] / "edge"
sys.path.insert(0, str(EDGE))

_TMP = Path(tempfile.mkdtemp(prefix="ceravis-enroll-media-"))
os.environ["DATA_DIR"] = str(_TMP)

from fastapi import FastAPI                                    # noqa: E402
from fastapi.testclient import TestClient                      # noqa: E402

from api import recipient_routes as routes                     # noqa: E402
from config.settings import settings                           # noqa: E402

FAILURES: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}{('  — ' + detail) if detail else ''}")
    if not cond:
        FAILURES.append(label)


JPEG = cv2.imencode(".jpg", np.full((720, 1280, 3), 90, np.uint8))[1].tobytes()
mgr = routes.enrollment_manager
RID = "5038"


class Frame:
    frame = cv2.imdecode(np.frombuffer(JPEG, np.uint8), cv2.IMREAD_COLOR)
    frame_id = 1


class Cameras:
    def get_frame(self, camera_id):
        return Frame() if camera_id == "CAM_01" else None


app = FastAPI()
app.include_router(routes.router)
app.state.camera_manager = Cameras()
http = TestClient(app)
BASE = f"/api/v1/recipients/{RID}"


def capture(label: str) -> dict:
    r = http.post(f"{BASE}/enroll/capture", json={"camera_id": "CAM_01", "label": label})
    assert r.status_code == 200, r.text
    time.sleep(0.005)       # photo names are the millisecond they were saved in
    return r.json()


def names() -> list[str]:
    return mgr.media_names(RID)


capture("front/standing")
body = capture("back/sitting")
by_label = {f["label"]: f["name"] for f in body["frames"]}
KEEP, GONE = by_label["front/standing"], by_label["back/sitting"]
mgr.import_photo(RID, "cloud_77.jpg", JPEG)
mgr.record_label(RID, "cloud_77.jpg", "left/sitting")

print("\nwhat may be deleted")
frames = {f["name"]: f for f in http.get(f"{BASE}/media").json()["frames"]}
check("captured pictures can be deleted, the app's cannot",
      frames[KEEP]["deletable"] and frames[GONE]["deletable"]
      and not frames["cloud_77.jpg"]["deletable"])
r = http.delete(f"{BASE}/media/cloud_77.jpg")
check("a picture from the app is refused and stays",
      r.status_code == 409 and mgr.has_photo(RID, "cloud_77.jpg"))
check("a name that is no photo deletes nothing",
      http.delete(f"{BASE}/media/nope.jpg").status_code == 404 and len(names()) == 3)
(mgr.base_path / RID / "status.json").write_text('{"state": "ready"}')
check("a path out of the photo folder deletes nothing",
      not mgr.delete_photo(RID, "../status.json")
      and not mgr.delete_photo(RID, "..") and not mgr.delete_photo(RID, "")
      and (mgr.base_path / RID / "status.json").exists())
check("another recipient's folder is not reached",
      not mgr.delete_photo("someone-else", GONE) and mgr.has_photo(RID, GONE))

print("\ndeleting a picture")
mgr.save_embeddings(RID, np.ones((2, settings.reid_embedding_dim), np.float32))
r = http.delete(f"{BASE}/media/{GONE}")
left = [f["name"] for f in r.json()["frames"]]
check("the picture is gone, the others stay",
      r.status_code == 200 and set(left) == {KEEP, "cloud_77.jpg"}
      and not (mgr.base_path / RID / "photos" / GONE).exists())
check("its label is gone, the others' labels stay",
      mgr.get_labels(RID) == {KEEP: "front/standing", "cloud_77.jpg": "left/sitting"},
      str(mgr.get_labels(RID)))
st = mgr.get_status(RID)
check("an enrolled recipient is back in review and told to Enroll again",
      st["state"] == "review" and "Enroll again" in st["message"], str(st))
check("the vectors already made are not touched by a delete",
      mgr.load_embeddings(RID).shape[0] == 2)
check("deleting it twice is a 404",
      http.delete(f"{BASE}/media/{GONE}").status_code == 404)
check("no temporary file is left next to the labels",
      not list((mgr.base_path / RID).glob(".labels*")))

p = mgr.save_photo("solo", JPEG)
r = http.delete(f"/api/v1/recipients/solo/media/{p.name}")
check("the last picture gone: nothing to review, status says to add media",
      r.json()["count"] == 0 and mgr.get_status("solo")["state"] == "none")

shutil.rmtree(_TMP, ignore_errors=True)
print()
if FAILURES:
    print(f"{len(FAILURES)} check(s) FAILED:")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("All enrollment media checks passed.")

#!/usr/bin/env python3
"""
Enrollment pictures in the setup wizard: deleting one, and face close-ups.

Deleting (the cross on a picture):
  the picture and its label go, the others stay; a name that is not a photo of
  this recipient (or tries to leave the folder) deletes nothing;
  a picture from the CERAVIS app is refused — it is removed in the app;
  an enrolled recipient goes back to 'review' and is told to Enroll again;
  the vectors already in the gallery are not touched by a delete.

Face close-ups (label "face/<view>"):
  the ONE rule for "a picture of one face" (none / two / too small);
  front = straight on, left / right = turned a little the person's own way,
  not the other way, not too far; a face too small is told to come closer;
  a refused close-up saves NOTHING and says what to change;
  an accepted one is saved with its label;
  a face close-up never enters the body gallery (the worker skips it), and an
  ordinary photo still does.

The face detector is a stand-in (YuNet rows written by hand); no model files,
no TensorRT. Runs on the dev box:

    python tests/test_enroll_media.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
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
from enrollment.enrollment_worker import EnrollmentWorker      # noqa: E402
from reid import face_identity as fi                           # noqa: E402

FAILURES: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}{('  — ' + detail) if detail else ''}")
    if not cond:
        FAILURES.append(label)


def face(width: float, turn: float = 0.0, x: float = 400.0) -> np.ndarray:
    """A YuNet row: box, right eye, left eye, nose, mouth corners, score. The
    right eye is the person's own — on the LEFT of the picture."""
    eye = width * 0.4
    cx, y = x + width / 2.0, 300.0
    rx, lx = cx - eye / 2.0, cx + eye / 2.0
    nx = cx + turn * eye
    return np.array([x, y, width, width * 1.3,
                     rx, y + 40, lx, y + 40, nx, y + 70,
                     rx, y + 100, lx, y + 100, 0.95], np.float32)


JPEG = cv2.imencode(".jpg", np.full((720, 1280, 3), 90, np.uint8))[1].tobytes()
mgr = routes.enrollment_manager
RID = "5038"


# ---------------------------------------------------------------- the face rules
print("\none clear face in a picture")
check("no face", fi.one_clear_face([], 40)[1] == "no face")
check("two faces of similar size are ambiguous",
      fi.one_clear_face([face(120), face(100, x=800)], 40)[1] == "two faces")
check("a small face in the background does not count as a second one",
      fi.one_clear_face([face(40, x=900), face(160)], 40)[0][2] == 160)
check("a face under the bar is too small",
      fi.one_clear_face([face(30)], 40)[1] == "face too small")

print("\nthe asked-for close-up")
MIN = settings.face_capture_min_px
check("turn is signed: positive = the person's own left",
      fi.turn(face(150, 0.3)) > 0 > fi.turn(face(150, -0.3))
      and abs(fi.yaw_ratio(face(150, -0.3)) - 0.3) < 1e-3)
check("front: straight on passes", fi.check_face_view(face(150, 0.05), "front") == "")
check("front: a turned head is told to look at the camera",
      "straight" in fi.check_face_view(face(150, 0.3), "front"))
check("left: turned a little to the person's left passes",
      fi.check_face_view(face(150, 0.28), "left") == "")
check("right: turned a little to the person's right passes",
      fi.check_face_view(face(150, -0.28), "right") == "")
check("left: looking straight is told to turn",
      "a little to the left" in fi.check_face_view(face(150, 0.02), "left"))
check("left: turned the other way is said so",
      "other way" in fi.check_face_view(face(150, -0.28), "left"))
check("turned further than a live look may be is too far",
      "too far" in fi.check_face_view(face(150, 0.6), "left"))
check("a face too small is told to come closer — whatever its turn",
      "come closer" in fi.check_face_view(face(MIN - 20, 0.3), "front"))
check("just inside the bars passes, just outside does not",
      fi.check_face_view(face(MIN, settings.face_capture_front_max_yaw - 0.01), "front") == ""
      and fi.check_face_view(face(MIN, settings.face_capture_front_max_yaw + 0.01), "front") != ""
      and fi.check_face_view(face(MIN, settings.face_max_yaw_ratio - 0.01), "left") == ""
      and fi.check_face_view(face(MIN, settings.face_max_yaw_ratio + 0.01), "left") != "")
check("an unknown view is refused", "unknown" in fi.check_face_view(face(150), "back"))
check("labels: face/<view> is a face label, a body pose is not",
      fi.is_face_label("face/front") and not fi.is_face_label("front/standing")
      and not fi.is_face_label("") and not fi.is_face_label(None))


# ---------------------------------------------------------------- the API
class Frame:
    frame = cv2.imdecode(np.frombuffer(JPEG, np.uint8), cv2.IMREAD_COLOR)
    frame_id = 1


class Cameras:
    def get_frame(self, camera_id):
        return Frame() if camera_id == "CAM_01" else None


class Finder:
    """Stand-in detector: answers the rows the test set."""
    ready = True
    faces: list[np.ndarray] = []

    def find_in_photo(self, img):
        return list(self.faces)


app = FastAPI()
app.include_router(routes.router)
app.state.camera_manager = Cameras()
routes._finder = Finder()
http = TestClient(app)
BASE = f"/api/v1/recipients/{RID}"


def capture(label: str, camera: str = "CAM_01"):
    return http.post(f"{BASE}/enroll/capture", json={"camera_id": camera, "label": label})


def names() -> list[str]:
    return mgr.media_names(RID)


print("\nface close-ups at capture")
Finder.faces = []
r = capture("face/front")
check("no face: refused with what to do, nothing saved",
      r.status_code == 422 and "no face" in r.json()["detail"] and names() == [],
      f"{r.status_code} {r.text[:80]}")
Finder.faces = [face(150), face(140, x=800)]
r = capture("face/front")
check("two faces: refused, nothing saved",
      r.status_code == 422 and "two faces" in r.json()["detail"] and names() == [])
Finder.faces = [face(70)]
r = capture("face/front")
check("too far from the camera: told to come closer, nothing saved",
      r.status_code == 422 and "come closer" in r.json()["detail"] and names() == [])
Finder.faces = [face(150, 0.3)]
r = capture("face/front")
check("turned when 'front' was asked: refused",
      r.status_code == 422 and "straight" in r.json()["detail"] and names() == [])
r = capture("face/up")
check("a view that does not exist is a bad request", r.status_code == 400)
r = capture("face/left")
body = r.json()
check("a good close-up is saved with its label and the face width",
      r.status_code == 200 and body["count"] == 1
      and body["frames"][0]["label"] == "face/left"
      and body["face"] == {"width_px": 150}, r.text[:120])
FACE_PHOTO = body["frames"][0]["name"]

routes._finder = type("Off", (), {"ready": False})()
r = capture("face/front")
check("face recognition off on the device: said so, nothing saved",
      r.status_code == 409 and len(names()) == 1)
routes._finder = Finder()

import time                                                    # noqa: E402
time.sleep(0.005)       # photo names are the millisecond they were saved in
Finder.faces = []
r = capture("front/standing")
check("a body pose is not face-checked (no face needed)",
      r.status_code == 200 and r.json()["count"] == 2 and r.json()["face"] is None)
BODY_PHOTO = next(f["name"] for f in r.json()["frames"] if f["label"] == "front/standing")
check("no frame from the camera is still a 404",
      capture("front/standing", "CAM_09").status_code == 404)

print("\nthe body gallery never takes a face close-up")
worker = EnrollmentWorker(mgr, gallery=None)        # no detector: whole images
crops, labels = worker._collect_crops(RID)
check("only the body photo is collected for body vectors",
      labels == ["front/standing"] and len(crops) == 1, str(labels))

print("\ndeleting a picture")
mgr.import_photo(RID, "cloud_77.jpg", JPEG)
mgr.record_label(RID, "cloud_77.jpg", "back/sitting")
frames = {f["name"]: f for f in http.get(f"{BASE}/media").json()["frames"]}
check("captured pictures can be deleted, the app's cannot",
      frames[FACE_PHOTO]["deletable"] and frames[BODY_PHOTO]["deletable"]
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
      not mgr.delete_photo("someone-else", BODY_PHOTO) and mgr.has_photo(RID, BODY_PHOTO))

mgr.save_embeddings(RID, np.ones((2, settings.reid_embedding_dim), np.float32))
r = http.delete(f"{BASE}/media/{BODY_PHOTO}")
left = [f["name"] for f in r.json()["frames"]]
check("the picture is gone, the others stay",
      r.status_code == 200 and BODY_PHOTO not in left
      and set(left) == {FACE_PHOTO, "cloud_77.jpg"}
      and not (mgr.base_path / RID / "photos" / BODY_PHOTO).exists())
check("its label is gone, the others' labels stay",
      mgr.get_labels(RID) == {FACE_PHOTO: "face/left", "cloud_77.jpg": "back/sitting"},
      str(mgr.get_labels(RID)))
st = mgr.get_status(RID)
check("an enrolled recipient is back in review and told to Enroll again",
      st["state"] == "review" and "Enroll again" in st["message"], str(st))
check("the vectors already made are not touched by a delete",
      mgr.load_embeddings(RID).shape[0] == 2)
check("deleting it twice is a 404",
      http.delete(f"{BASE}/media/{BODY_PHOTO}").status_code == 404)
r = http.delete(f"{BASE}/media/{FACE_PHOTO}")
check("a face close-up is deleted the same way",
      r.status_code == 200 and [f["name"] for f in r.json()["frames"]] == ["cloud_77.jpg"])
check("no temporary file is left next to the labels",
      not list((mgr.base_path / RID).glob(".labels*")))

mgr.delete_photo(RID, "cloud_77.jpg")       # the store itself has no such rule
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

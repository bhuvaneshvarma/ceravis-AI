# CERAVIS — Session Memory

This document is a chronological record of **one Claude Code conversation**
covering: GitHub setup, Jetson Orin Nano native deployment (Docker → native
migration), YOLO26 detection debugging, pipeline optimization, and ReID/
enrollment build-out. It exists so a future conversation (human or AI) can
understand *why* the code looks the way it does without re-deriving it.

> **Note on git history:** at the end of this session, `git log` on the
> `ceravis1.1` branch showed commits unrelated to this conversation (cloud/
> AWS alert-forwarding work, e.g. `efd944f "Focus all AI on the recipient's
> camera"`). This session's own commits (`a7e2f74`, `ce1ecbc`, `95c0657`,
> `340ec7d`, `32ac82a`, …) were not visible in that log. This suggests a
> separate/parallel session touched this repo, or the branch was reset.
> **Verify `git log --all` and the GitHub remote before trusting any
> "current state" claim below** — this file documents what was *decided and
> built in this conversation*, which may or may not match what's on disk now.

---

## Phase 1 — GitHub repository setup

**Prompt:**
> "okay now tell me we shall use rsync or i directly on jetson login and clone the repo. If by rsync tell the procedure"

> "what is your preferred protocol for git operations?"

**Context (from earlier in the session, before this transcript):** the local
folder was renamed `ceravis2` → attempted rename to `ceravis1.1` (blocked by
an OS-level lock — OneDrive/session holding the directory open). A GitHub
repo was created: **`bhuvaneshvarma/ceravis-AI`** (private), via installing
GitHub CLI (`gh`) through winget and a device-code browser login. Initial
commit pushed to a `main` branch, secrets scrubbed (`edge/data/cameras.json`
— real RTSP credentials — gitignored, `cameras.example.json` added instead).

**Decisions:**
- Recommended **HTTPS** protocol for git (works through firewalls, `gh`
  handles token storage, no SSH key setup needed).
- Recommended **cloning directly on the Jetson** via `gh repo clone` rather
  than rsync from Windows — simpler, and future updates are just `git pull`.

**Branching:** user asked to rename `main` → `ceravis1.1` as the "first
version" branch, intending to branch-per-version for future upgrades.
Pushed back gently: the industry-standard pattern is **one trunk branch +
git tags** for releases (e.g. `v1.1`, `v1.2`), not a new branch per version.
Compromise: renamed `main` → `ceravis1.1` (default branch) **and** added an
annotated tag `v1.1` pointing at the same commit, recommending tags for all
future releases while keeping `ceravis1.1` as the ongoing trunk.

---

## Phase 2 — Docker build debugging (Jetson, JetPack 6.x)

**Prompt:**
> "cd ~/ceravis2 / docker compose -f docker-compose.jetson.yml build / ... So why does it download jetpack base image? we have already installed all the jetpacks from the sdk manager only right"

**Answer:** Docker containers are isolated from the host filesystem — JetPack
installed via SDK Manager lives on the **host**, but a container starts from
a blank slate and needs its own copy of CUDA/TensorRT/etc., which is what the
`l4t-jetpack` base image provides. One-time ~3 GB download per device.

**Prompt:**
> "okay so while building I got a error permission denied while connecting to the docker API at unix:///var/run/docker.sock"

**Fix:** `sudo usermod -aG docker $USER && newgrp docker` (user wasn't in the
`docker` group).

**Prompt (re-asked the same base-image question, then):**
> "So i the future if we install for a new recipient with a new edge device we dont need to install the jetpacks too / Only the OS is enough right"

**Answer:** No — JetPack must still be flashed on every new device. Docker's
base image provides *userspace* libraries; the **kernel GPU driver** still
must come from the host JetPack flash. Docker doesn't eliminate the
per-device SDK Manager step.

**Prompt:**
> "=> ERROR [2/7] RUN apt-get update && apt-get install ... python3-pycuda ... Package 'python3-pycuda' has no installation candidate"

**Fix:** `python3-pycuda` doesn't exist as an apt package in the
`l4t-jetpack:r36.3.0` base. Removed it from the apt install list; added
`pycuda` to `requirements.ai.txt` (pip) instead.

**Prompt:**
> "shall i install a dockerdesktop or not required rn?"

**Answer:** Not required — Docker Engine + `nvidia-container-runtime` are
already on the Jetson via JetPack; Docker Desktop is a Windows/Mac GUI
wrapper, would only waste RAM on-device.

**Prompt:**
> "ERROR [5/7] RUN pip3 install --no-cache-dir --break-system-packages ... no such option: --break-system-packages"

**Fix:** The base image's pip (≈22.x) predates that flag (Python 3.12+
only; the base ships Python 3.10). Removed the flag, added
`pip3 install --upgrade pip setuptools wheel` before installing
requirements. Also asked: *"can we also future-proof if any such other
errors are expected?"* — added `build-essential`, `cmake`, `swig` to the
apt layer (needed if `pycuda`/`faiss` compile from source on ARM64), and
fixed a stale comment.

**Prompt:**
> "also the versions i think we shall not mention any in the requirements, let it first install and then we can freeze them"

**Action:** Removed all `==`/`>=` version pins from `requirements.base.txt`
and `requirements.ai.txt` — unpinned, to freeze later via `pip3 freeze`
after a successful build.

**Prompt (the big one — torch/CUDA failure):**
> "cd ~/ceravis2 && docker compose -f docker-compose.jetson.yml up / Ran this an i got this: [ceravis] export_models.py … / ... ValueError: Invalid CUDA 'device=0' requested ... torch.cuda.is_available(): False ... ImportError: cannot import name 'UTC' from 'datetime' ... / What is this error / after solving it if we build new,, then lets also clear the earlier everything too the cache memory unused or removed or no more use installed things from earlier build"

**Two root causes diagnosed:**
1. `ImportError: cannot import name 'UTC'` — the **running container code
   was stale** (predated a `timezone.utc` fix already committed); fixed by
   `git pull` on the device (the file is volume-mounted).
2. `torch.cuda.is_available(): False` — pip had installed PyPI
   `torch==2.12.0+cu130` (CUDA 13, no Jetson GPU support) because the
   Dockerfile's `--index-url` pointed at a non-functional NVIDIA redist URL
   and pip silently fell back to PyPI.

**Fix:** Rewrote the Dockerfile's torch install to pull from
`pypi.jetson-ai-lab.dev/jp6/cu126` via `--index-url` (forcing the Jetson
wheel, not PyPI), plus a build-time assertion
(`torch.version.cuda is not None`) so a wrong wheel fails the *build*, not
silently at runtime.

**Prompt:**
> "the build is taking too much time for the downloads of jetpack and other too / l4t-jetpack:r36.3.0, ~4 GB it got installed until now only 400mb out of 3.52 gb / So even if i restart the edge device still? / ... If we need to restart this then again we need to clear all the unused earlier things or waste shall be cleared completely"

**Explained:** Docker layers are cached once *fully* completed; a restart
only loses the one in-flight layer, not finished ones. Advised: **don't**
prune/restart unnecessarily — the base image, once pulled, is permanent
per-device.

**Prompt:**
> "i just restarted actually, and the download its started from beginning / Now the earlier downloaded ones are cleared or gets stored in cache? or anywhere else?"

**Explained:** The in-flight multi-GB layer is discarded on daemon
restart/reboot — Docker has no resume-partial-layer feature. Recommended
running the build inside `tmux` or as a backgrounded `docker compose build`
so an SSH disconnect or reboot doesn't kill it again.

**Prompt:**
> "now this thing will be running entirely or can i stop it and run it whenever i want?"

**Explained** `systemctl stop/start/restart/enable/disable ceravis` controls
— full control over the running service, no work lost by stopping it.

---

## Phase 3 — The big pivot: drop Docker, run native on JetPack

**Prompt:**
> "First of all we need to use the default jetpacks tools only / We need to plan accordingly at any cost to use those than custom install again as it takes most space more time, build complexity more and many more bottlenecks / Let us see for any other best ways"

**Key insight surfaced:** the app's inference code (`trt_engine.py`,
`yolo_detector.py`, `yolo_pose.py`) runs **pure TensorRT + PyCUDA at
runtime — torch is never used for inference**, only for the one-time
ONNX export. That meant Docker (and its duplicate ~6 GB CUDA/TensorRT
stack) wasn't actually necessary.

**Architecture change — went fully native, no Docker:**
- `edge/scripts/export_models.py` rewritten: two stages —
  (1) ONNX export via ultralytics (needs CPU torch, done in a disposable
  venv), (2) ONNX → FP16 engine via JetPack's own `trtexec` (zero Python
  deps).
- `edge/scripts/install_native.sh` — apt + pip deps using JetPack's own
  numpy/opencv/TensorRT (never reinstalled via pip).
- `edge/scripts/export_engines.sh` — disposable CPU-torch venv
  (`/tmp/ceravis-export-venv`, deletable after) drives stage 1; stage 2
  (`trtexec`) runs **in a separate, torch-free process** afterward.
- `edge/scripts/install_service.sh` + `infra/systemd/ceravis.service` —
  boot-time systemd unit, `Restart=on-failure`, `ExecStartPre` rebuilds
  missing engines.
- Deleted: `Dockerfile.jetson`, `docker-compose.jetson.yml`,
  `entrypoint.sh` (recoverable from git history / the `v1.1` tag).
- `settings.py` / `jetson.env`: container paths (`/models`, `/app/data`)
  changed to repo-relative paths; `env_file` resolved relative to the
  settings module's location (works regardless of launcher: systemd,
  shell, IDE).
- Net effect: ~6 GB of container pulls replaced with <2.5 GB total
  (~1.5 GB of which is the deletable export venv).

**Prompt (requirements consolidation):**
> "why do we have multiple requirements / Cant we use one only? are we using them all?"

**Action:** Audited all three requirement files; merged
`requirements.base.txt` + `requirements.ai.txt` into a single
`edge/requirements.txt`; deleted the unused dev/x86 `Dockerfile` and
`docker-compose.dev.yml` and `infra/env/edge.env` (laptop dev path was never
actually used in this project).

---

## Phase 4 — First native build errors, sanity audit

**Prompt (multiple build failures in sequence):**
> "Cuda failure: no CUDA-capable device is detected / [error] trtexec failed (rc=1) for yolo26m-pose.engine / ... Job for ceravis.service failed because a timeout was exceeded"

**Root causes + fixes:**
1. The pose engine's `trtexec` build failed with a CUDA init error because
   the CPU-torch venv (~2 GB resident) was still loaded while `trtexec` ran
   as its child — fixed by **splitting export into two passes**:
   `export_models.py --onnx-only` inside the venv, then a **fresh,
   torch-free** `python3 scripts/export_models.py` for the engine build
   (this is the change described above in `export_engines.sh`).
2. `ceravis.service` timed out because `ExecStartPre` (which can rebuild a
   missing engine, taking minutes) exceeded systemd's default 90 s start
   timeout — fixed with `TimeoutStartSec=900` in the unit file.

**Prompt (full sanity check after a fresh JetPack 6.2.2 flash):**
> "so lets once more sanity check everything / Clear all the earlier installed useless and cache builds ... check all the dependencies and tools which we want are present or not and are right versions"

**Built `edge/scripts/check_jetson.sh`** — a read-only doctor script
verifying: L4T/JetPack version, `nvcc`, TensorRT (python + `trtexec`),
OpenCV-with-GStreamer, numpy, GStreamer decode elements, every pip runtime
dep, exported models, swap/disk/power-mode, and the systemd service —
printing `[OK]/[WARN]/[FAIL]` with the exact fix command for each failure.

**Found via the doctor:** `trtexec` (the `libnvinfer-bin` package) was
missing because SDK Manager's "Jetson SDK Components" section had been
unchecked during flashing — fixed with
`sudo apt install -y libnvinfer-bin`.

**Prompt (unify the scripts):**
> "And accordingly in the scripts dont make multiple scripts to run / Anytime give one thing to run multiple things make lose progress / Unify"

**Built `edge/scripts/setup.sh`** — single entrypoint chaining
deps → engines → service → doctor, every stage idempotent (re-running after
a fix never repeats finished work).

**Prompt:**
> "and dont create this swapon / we dont need this i presume"

**Action:** Removed the swapfile-creation stage from `setup.sh` entirely —
Jetson's default ~3.8 GB zram was judged sufficient; doctor's swap check
relaxed to accept zram-only as `[OK]`.

---

## Phase 5 — Detection is "not working" (the long debugging arc)

**Prompt (huge requirements dump + first report of broken detection):**
> "okay everything went very well... But we need to make more optimization and checks / And also im unable to test the AI engine / the yolo detection is not working... / we dont have a person/recipient registration setup right..."

This message also restated the **full original project requirements**
(multi-camera RTSP, person detection/tracking/ReID, posture recognition,
camera labeling incl. `*` bathroom / `&` entrance markers, zone
subdivision, PTZ, motion-duration-tiered capture rules — 5/10/30 s
thresholds, 3-snapshot summaries, visitor detection, 30-minute inactivity
checks, fall-vs-lying-down distinction, and a recommended production
enrollment architecture: *Resident Enrollment → Store Media → Queue
Embedding Generation → Worker Creates Embeddings → FAISS Updated*).
Scope agreed for this pass: **finish sitting/standing/walking/fall +
enrollment**; defer motion-duration/snapshot/visitor/inactivity systems to
a later phase.

**Built (this pass):**
- `edge/enrollment/enrollment_manager.py` — on-disk recipient media store
  (photos/videos/embeddings/status.json), exactly matching the
  user-specified architecture.
- `edge/enrollment/enrollment_worker.py` — background queue: crops the
  largest detected person from each photo/video frame, embeds via the ReID
  extractor, rebuilds the shared FAISS gallery; degrades gracefully to
  `pending_reid` if the ReID engine isn't built yet (media is kept, nothing
  lost).
- `edge/api/recipient_routes.py` — `POST enroll/photos`,
  `enroll/video`, `enroll/live` (captures frames from a live camera),
  `GET enroll/status`.
- `edge/schemas/recipient.py` extended: `emergency_contacts`, `device_id`,
  `date_of_birth`, `address`, `medical_notes`.
- `main.py`: FAISS gallery instantiated independently of the ReID runner
  and shared with the enrollment worker.
- New UI pages, shared design system: `static/ceravis.css` + `ceravis.js`
  (premium dark theme), `static/setup.html` (4-step wizard: cameras → zones
  → recipient/enrollment → review), `static/live.html` (surveillance-grid
  live wall, became the new `/` redirect target), `static/monitor.html`
  (AI monitor: live bounding-box + posture overlay, activity log, event
  history).
- `api/ai_routes.py` (`/api/v1/ai/state` — merged track+posture+identity
  per camera) and `api/event_routes.py` (`/api/v1/events` — recent
  rule-engine events from SQLite) added to support the monitor page.

**Diagnosed but NOT yet fixed in this pass:** detection produced
`detections_generated: 0` across 7000+ processed frames. Initial fix
attempt (later found incomplete — see Phase 6): rewrote
`yolo_detector.py` to auto-detect the engine's output layout (YOLO26
end-to-end `(1,300,6)` vs a raw anchor-grid head) since the format had
never been empirically confirmed against the real engine output.

---

## Phase 6 — Throughput/load optimization

**Prompt (the big optimization ask):**
> "Another most important thing coming to the video stream, it is capped to 5fps right... for the AI inference engine we need more than 5fps... remember for reducing the load we need to optimize all the workflows... after detecting, the bounding boxes... only these are sent to next tracking and reid... as the target person is identified by reid/faiss, then we need to omit the other identified persons too... for this we need botsord then implement that too... best accuracy over best utilization of the GPU... without heavy unnecessary use... using only upto what are needed"

**Decoupled per-stage frame rates** (the 5 fps cap was the literal RTSP
capture rate, throttling everything downstream):

| Stage | Before | After |
|---|---|---|
| Capture | 5 | 15 |
| Stream (UI) | 10 | 15 |
| Detection | 5 | 10 |
| Pose | 2 | 12 |
| ReID | 2 | 3 |

**Built:**
- `edge/common/crops.py` — shared padded person-crop helper (`pad_frac`),
  used by both ReID and pose so only the person's pixels (with a small
  margin) are ever processed downstream, never the full frame/background.
- `edge/reid/target_registry.py` — per-camera "who is the locked target"
  registry with a TTL. Once FAISS/ReID matches a track to the enrolled
  recipient, that track_id is locked; pose then runs **only on the
  target's crop**, and ReID stops re-embedding everyone else while the
  lock is fresh. Re-acquires the lock if the target's track is lost and
  reappears under a new ID (occlusion recovery via re-matching against the
  gallery).
- `pose/pose_runner.py` rewritten: **idle-gated** (zero pose inference for
  a camera with no tracked people — an empty room costs nothing) plus the
  target-focus crop path above, falling back to full-frame pose
  pre-lock / when ReID is off.
- CORS middleware added to `main.py` (`allow_origins=["*"]`) plus
  LAN-IP logging at startup, so an external/off-device frontend can call
  the API (the server already bound `0.0.0.0:8000` — `localhost` access
  failures were a client-side habit, not a server limitation).

**Honest engineering pushback on the literal asks:**
- *"Send crops to tracking too"* — explained ByteTrack is purely
  geometric (operates on `[x1,y1,x2,y2,score]`, never touches pixels), so
  routing crops to tracking would add work for zero benefit. Crops matter
  for ReID/pose, not tracking.
- *"Implement BoT-SORT"* — explained BoT-SORT's real runtime engines
  (ultralytics built-in, `boxmot`) require **PyTorch at runtime**, which
  directly conflicts with the Phase 3 decision to eliminate torch from the
  runtime entirely. Substituted the `TargetRegistry` (FAISS-based target
  lock) as a torch-free equivalent delivering BoT-SORT's main practical
  benefit (appearance-based occlusion recovery) for static in-home cameras,
  while noting camera-motion compensation (BoT-SORT's other feature) barely
  matters for fixed cameras.
- *"30 fps capture"* — explained that maximizing FPS blindly would waste
  GPU; the chosen rates (10 fps detection, 12 fps pose) are "the lowest
  rate that doesn't miss the event," which is the actual professional
  target, not "as high as possible."

---

## Phase 7 — Detection root-cause found; ReID engine choice

**Prompt (diagnostics requested in the previous turn came back):**
> "currently im only testing the system with some test rtsp links... ip of those links has changed... [pasted systemctl status, showing GStreamer reconnect warnings — a red herring, not the real bug]"
>
> Then, after running the actual diagnostics:
> `detector output shape=(1, 300, 6) -> layout=e2e`
> `frames_processed: 7018, detections_generated: 0`
>
> "So the thing is without changing anything tell me how to change only the rtsp links... also did the git pull work and did it restart fresh with the changes? how to check that too"

**Diagnosis confirmed:** the engine output format WAS correct
(`(1,300,6)` end-to-end, 9 ms latency) — the bug was in **preprocessing**,
not parsing.

**Root cause, finally identified:** `cv2.dnn.blobFromImage` was **squishing**
non-square camera frames directly to 640×640 with no aspect-ratio
preservation, distorting people enough that detection scores collapsed
below threshold. YOLO models are trained/exported expecting **letterboxed**
(aspect-preserving, padded) input.

**Fix:**
- `edge/common/letterbox.py` — shared aspect-preserving letterbox +
  blob-conversion helper, with the math to map model-space boxes/keypoints
  back to original-frame coordinates.
- `yolo_detector.py` rewritten again: uses letterbox; also made the
  end-to-end parser **auto-detect which of the last two output columns is
  the confidence score vs. the class id** (score is a float in [0,1], class
  is an integer) rather than assuming a fixed column order — removes a
  second latent bug.
- `yolo_pose.py`: same letterbox fix applied, keypoints un-letterboxed back
  to frame coordinates.
- Added one-time diagnostic logging: real output shape, chosen layout,
  chosen score column, and the top prediction row + max score + kept count
  — so this class of bug is now directly verifiable from the journal
  instead of being inferred.

**ReID engine choice — FastReid → OSNet pivot:**
FastReid's ONNX isn't pip-obtainable (needs the fast-reid GitHub repo + a
checkpoint + their own export tool, run on a workstation) and its BoT_R50
backbone is heavy for an 8 GB shared-memory board. Switched the default
ReID backbone to **OSNet x1_0 via `torchreid`** — ~5× lighter, exports its
ONNX with one `pip install torchreid` command, excellent accuracy for
in-home ReID.

**Built:**
- `edge/reid/reid_extractor.py` — generic TensorRT embedder (replaces the
  FastReid-specific one), works for either backbone via the embedding-dim
  setting; embeds a **padded crop only**, never background.
- `edge/scripts/export_reid.sh` — disposable CPU venv exports OSNet → ONNX
  via torchreid, then JetPack's `trtexec` builds the FP16 engine
  (torch-free), same two-pass pattern as the detect/pose export.
- `settings.py` / `jetson.env`: `reid_model_name`, `reid_onnx_path/url`,
  `reid_embedding_dim` (512 for OSNet vs 2048 for FastReid) — model-agnostic
  so FastReid can still be dropped in later if ever wanted.
- Matching stays **FAISS** (`IndexFlatIP`, cosine) — torchreid only
  produces the embedding; FAISS is the fastest correct matcher for a small
  per-home gallery.

---

## Phase 8 — Finishing ReID, FastReid purge, standalone tester, enrollment review UI

**Prompt:**
> "okay so we have upgraded to the torchreid now right for the reid?? So if yes then completely remove the fastreid related all the stuff and just lets use the osnet reid x1.0 for extraction and then for matching whichever is fastest lets use it either the torchreid or the faiss / ... there was a small error on the no module named scipy error / Fix that too... remove all the files downloaded stuff related to the fastreid and also clear the cache and memory related to it / ... Make a separate module file which only takes the saved camera rtsp link and access the live video stream and runs yolo detect... a simple clean small module file which ill execute in the terminal itself which pops up a small video window... / And then also update a small context box where the media option is selected and also for the live view which takes 4 seconds video to extract frames, we shall be able to see those frames for once cross check, once we enroll and save the extracted embeddings after checking the frames by clicking the enroll button it saves the embeddings and also few frames of the registered person as jpeg... Upgrade these changes and make a single .sh file to update all these upgrades and also remove the cache and other leftovers of the fast reid too"

**Confirmed:** yes, OSNet/torchreid is the locked-in default; matching is
FAISS (clarified torchreid has no matcher of its own).

**Built/changed:**
- Deleted `reid/fastreid_extractor.py` entirely; verified zero remaining
  code references to FastReid anywhere (`grep` confirmed clean — only
  historical comments mention it).
- `edge/scripts/test_detect.py` — the standalone tester requested: opens a
  registered (or directly-supplied) RTSP stream, runs the real
  `YOLODetector`, draws boxes in a `cv2.imshow` popup window if a display is
  attached, or writes `/tmp/ceravis_detect.jpg` + prints per-second counts
  when run headless over SSH. Accepts an optional confidence-threshold
  override.
- **Enrollment flow restructured** to a preview → review → commit pattern
  (previously photos/video/live auto-enrolled immediately):
  - `enroll/photos`, `enroll/video`, `enroll/live` now **only store** media
    and return frame preview URLs (status becomes `review`).
  - New `enroll/commit` endpoint actually queues the embedding worker.
  - `enrollment_manager`: `media_names()`, `media_path()` (path-traversal
    safe lookup), `save_reference_crops()` — persists a handful of small
    JPEG crops of the enrolled person to `body/crops/` for future
    reference.
  - `setup.html` step 3: added a per-tab **context box** explaining each
    enrollment option (A/B/C), a **thumbnail review grid** of captured/
    uploaded frames, and a separate **"Enroll & save embeddings"** button
    that only becomes active once frames exist — decoupling capture from
    commit so the user can visually verify frames first.
- `edge/scripts/install_native.sh`: fixed the scipy import failure by
  installing `python3-scipy` + `python3-matplotlib` via **apt** (prebuilt
  aarch64, matches system numpy ABI) instead of pip, with a pip fallback
  (`scipy<1.14`) and a **hard import gate** that fails the script loudly if
  `supervision`/`scipy` still won't import — so this class of failure can't
  silently slip through again.
- `edge/scripts/upgrade.sh` — the single requested upgrade script: pulls
  latest code, purges FastReid leftovers (`models/reid/fastreid*`, the
  export venv, pip cache, `__pycache__`), runs `install_native.sh`, builds
  the OSNet ReID engine via `export_reid.sh`, restarts the service, runs the
  doctor script to verify.

---

## Phase 9 — scipy STILL missing after the fix; interpreter-mismatch diagnosis

**Prompt:**
> "even after this: [ran the 4-step manual fix] / Still the same issue / Check thoroughly what is the issue, whats happening and lets overcome the issue professionally and industry grade production ready way / Lets gooo / Leave the ssh thing aside / Lets continue our earlier traditional way"

**Diagnosis (in progress at time of writing):** the leading hypothesis is a
**Python interpreter mismatch** — the interactive shell's `python3` may
resolve to a different interpreter than `/usr/bin/python3`, which is what
`ceravis.service`'s `ExecStart` actually uses. If scipy was pip-installed
for the shell's `python3 --user` site-packages but the service runs a
different `/usr/bin/python3`, the install would appear to succeed in the
terminal while the service still fails. Diagnostic commands given:
`which -a python3 pip3`, `grep ExecStart /etc/systemd/system/ceravis.service`,
and installing explicitly via `/usr/bin/python3 -m pip install --user
"scipy<1.14" matplotlib` (with a `--break-system-packages` fallback note for
externally-managed-environment errors). **Not yet confirmed resolved as of
this memory file being written** — this is the open thread to pick up next.

**Note:** mid-way through this phase, system reminders revealed several
files had been modified by the user/another process beyond what's narrated
above (`scripts/test_reid.py` — a ReID self-test wired into `upgrade.sh`;
`enrollment_manager.get_labels/record_label` + `embedding_stats` — labeled
live-capture frames and an embedding-stats endpoint; `common/floor_reference.py`
imported into `pose_runner.py` for a "scene-aware fall: ground reference"
concept; tweaks to `target_registry.py`, `ceravis.css`, `event_routes.py`).
These represent real, intentional further iteration on enrollment UX and
fall-detection accuracy that this document did not originate — **read those
files directly for their current behavior** rather than trusting this
summary for them.

---

## Open items / where to pick up next

1. **Resolve the scipy/supervision interpreter-mismatch** (Phase 9, unresolved).
2. **Confirm the letterbox detection fix actually works on-device** — run
   `python3 scripts/test_detect.py` and confirm `persons > 0` with someone in
   frame; this was the single biggest blocker before optimization work began.
3. **Build/verify the OSNet ReID engine** (`scripts/export_reid.sh`) and
   confirm enrollment embeddings generate end-to-end (review grid → commit →
   `ready` state → reference JPEGs saved).
4. **Reconcile git state** — confirm whether this session's commits exist on
   the remote/branch the user is actually deploying from; the discrepancy
   noted at the top of this file is unresolved.
5. **Deferred from the big requirements dump (Phase 5):** motion-duration
   tiered capture (5 s/10 s/30 s thresholds, 3-snapshot summaries), visitor
   arrival/departure detection, 30-minute-inactivity snapshots, room-zone
   transition capture, PTZ control, blur/off-limits zone marking, camera
   special-designation markers (`*` bathroom, `&` entrance). None of this is
   built yet — sitting/standing/walking/fall + enrollment was the agreed
   scope for this session.
6. **Fleet/production update mechanism** (for scaling to many recipient
   homes) was discussed conceptually (AWS IoT Core Jobs recommended as the
   best fit, given the architecture already plans IoT Core for alerts) but
   not implemented — explicitly deferred until a real multi-device fleet
   exists.

---
---

# SESSION 2 — continuation (ReID made to work end-to-end → alerts, spatial, people-model, low-latency streaming)

> **Connecting the dots:** this session began exactly where Session 1's open
> items left off (scipy, detection letterbox unconfirmed, ReID engine/enrollment
> unverified) and resolved them, then built the alerts/spatial/visitor layers and
> a low-latency streaming overhaul.
>
> **Git-author note (resolves the Session 1 "unrecognized commits" mystery):**
> the user **re-commits Claude's changes under their own name** so `git log` does
> not show Claude as author/editor. So any commit hash that looks unfamiliar
> (e.g. `3d8a6ec`, `7854374`) is **our work, re-committed** — not a parallel
> session. The Session-1 "verify git state" caveat is therefore benign.
>
> **Stable baseline tag: `v1.1-stable` (commit `4ee02fe`)** — the point where ReID
> works end-to-end on the Jetson. All Session-2 feature commits are on top of it
> on branch `ceravis1.1`. **MEMORY.md is intentionally untracked / never committed.**

## S2.1 — Why ReID was actually broken (the real root cause)
`scripts/test_reid.py` passed on the **main thread** but the enrollment worker
failed with **"invalid device context - no currently active context."** Root
cause: **PyCUDA's CUDA context is thread-bound.** `import pycuda.autoinit` created
a context only on the main thread; engines built/used in background threads
(enroll-worker, reid-runner, detection/pose runners) had **no active context**.
**Fix (`detection/trt_engine.py`):** dropped `pycuda.autoinit`; retain the device
**primary context** (shareable across threads) once and `push()/pop()` it around
engine construction **and** every `infer()`. This was the true blocker behind the
Session-1 ReID symptoms.

Also hardened:
- **Enrollment worker**: stopped latching the first failed engine-load
  (`_engines_tried` removed → retries each job); surfaces the **real** exception
  (was hidden behind a generic "run export_reid.sh"); **auto-resumes** recipients
  left in `pending_reid`/`queued`/`error` on startup (no manual re-enroll).
- **Path hardening**: engine + data paths resolve against the **edge/ root**, so
  cwd never matters (`trt_engine.py`, `enrollment_manager.py`).
- `export_reid.sh`: `--system-site-packages` venv + scipy/matplotlib/onnxscript
  export deps (this is what finally cleared Session-1's scipy issue, on-device).
- `scripts/test_reid.py`: self-test now runs **off the main thread** so a pass
  actually proves the worker path.

## S2.2 — Posture accuracy (`bc845ab`, `0f3111a`)
- **Walking de-sensitized**: motion is now **scale-normalized** (fraction of the
  person's own torso length / sec) + an absolute pixel floor + **N-frame
  confirmation** → a chair-swivel/turn near the camera is no longer "walking".
- **Sit/stand view-invariant + flicker-proof**: decided from the **hip-knee-ankle
  joint angle** (view-invariant on a tilted ceiling cam); when **legs leave the
  frame** it returns `UNKNOWN` and the tracker **holds** the last posture instead
  of flipping to standing; sit↔stand only switches when **head vertical motion
  corroborates** it (head rises → stand, falls → sit). Runtime-verified.

## S2.3 — AI Monitor target-focus (`f4619fc`, `0f3111a`)
Removed the camera selector. Monitor polls all-camera `/ai/state`, **auto-follows
the camera the enrolled recipient is on** (sticky), and draws **only the single
highest-`reid_score` target box** (other people ignored); "🔍 searching" overlay
when no target. `ai_routes` now exposes `reid_score` + `view_label`.

## S2.4 — Enrollment: guided multi-view (`6c271ed`, `a2c90c0`)
"C · Live camera" tab → live preview + **red Capture** button + view/posture label
pickers; a **front/back/left/right × standing/sitting coverage grid** gates the
Enroll button (auto-advances, must cover all 8). New `POST /enroll/capture`
(frame + label sidecar). Labels flow into per-embedding metadata
(`labels_emb.json`). FAISS gallery now carries a **per-vector view label**, and
`search()` returns the matched view → live **snapshot view annotation**.

## S2.5 — Adaptive (online-learning) ReID (`2882b78`, `c6532fa`, `50aa44e`)
While the target is matched with **high confidence**, body embeddings are captured
live (**vectors only, no frames**) into a per-recipient `adaptive.npy` that **joins
the matching gallery** → robust to clothing changes. Safeguards: enrolled
embeddings never overwritten; gated by score; **diversity-preserving retention**
(evict the most-redundant vector, not FIFO; cap 100) so distinct outfits survive;
`/recipients/{id}/embeddings/stats` + logs for visibility. **FPS-lag fix
(`c6532fa`)**: the adaptive disk work was on the inference tick → moved to a
**throttled background thread** (queue hand-off) so it never starves capture/stream.

## S2.6 — Streaming / latency overhaul (`35b4898`, `7854374`→`109bc67`)
- `appsink drop=true max-buffers=1 sync=false` → always read the **freshest**
  frame (killed a queued-frame lag bug); JPEG encode moved **off the event loop**
  (`asyncio.to_thread`); optional `STREAM_MAX_WIDTH` downscale.
- **Auto per-camera RTSP transport** (`RTSP_TRANSPORT=auto`, default): at connect
  the reader runs `ip route get <cam-ip>`, and a **WIRED** egress → **UDP + 50 ms**
  jitter buffer (minimal lag), **WIRELESS** → **TCP + 200 ms** (no macroblock
  corruption), unknown → TCP. Resolved live, **never persisted**. This fixed the
  directly-connected Ethernet camera being laggy on TCP (ping 0% loss, `ffplay
  -fflags nobuffer -flags low_delay -rtsp_transport udp` was smooth → we now match
  it). Manual override still possible via `Camera.transport` / global setting.

## S2.7 — Alerts phase (`e9494f1`, `d3edce1`)
- **EventEnricher** (run by RuleEngine before publish): fills room (camera config)
  + **area** (`common/zone_resolver.py` point-in-polygon on the person's **foot
  point**) + **severity/title/message** + an **annotated snapshot** saved
  **S3-mirrored**: `data/events/<device_id>/<YYYY-MM-DD>/<event_id>.jpg`.
- **Zone-aware fall**: a fall whose foot is in a **rest zone** (bed/couch/…,
  **whole-word** matched so "bedside floor" stays a fall) → `lying_down` (info, no
  alarm); floor/elsewhere → **critical**.
- `Event` schema + `EventStore` gained `track_id/severity/title/message/
  acknowledged/ack_*` (idempotent column migration). API: `GET /events/{id}/
  snapshot`, `GET /events/snapshot?path=`, `POST /events/{id}/ack`.
- **AlertBroadcaster** → `WS /alerts/stream`; `monitor.html` shows a real-time
  severity banner (critical pulses + beeps) with snapshot thumbnail, **View
  camera** (pins live view 20 s) and **Acknowledge**.

## S2.8 — Spatial layer (`8153916`)
**SpatialRule** (replaced the pixel `InactivityRule`): `area_transition`
(recipient moving between two **named** zones, debounced) + **area-aware
`inactivity`** (recipient alone + no area change for `INACTIVITY_SECS`=30 min,
re-emitted = the periodic activity-snapshot cadence). Body-relative metric note:
"head moves 25 cm" ≈ 0.5 torso-lengths (the scaling is in place; the *capture* is
deferred snapshot-tier work).

## S2.9 — People model #1 (`ae976a0`)
**VisitSessionRule** (replaced `VisitorRule`): home-level `visitor_arrival` →
periodic `visitor_present` → `visitor_departure` with **onsite duration**.
Identity-agnostic (detects *whether* a visitor is present). **Increment 2 =
visitor IDENTITY via body ReID** (register family/caregiver/aide → separate
visitor gallery, recipient ReID untouched) is **DEFERRED** — paused for on-device
smoke-testing first (user's choice).

## S2.10 — Discussions / decisions (not all code)
- **Cloud architecture (CERAVIS Health):** edge = local AI; cloud = thin
  alert/control; **live video relayed on-demand (WebRTC), never stored** (the
  Ring/Nest model); **S3** for alert clips/snapshots (per-device day-by-day
  folders — our local layout already mirrors it), **DynamoDB** for records, **IoT
  Core** (mTLS) for device identity + alerts. Recipient provisioning: enter
  **email+mobile** at edge setup → fetch the rest from the main app's
  **`enrollment` table**; alerts post to an **`alerts` table**. **Storage seam =
  local now, swap to DB/S3 later** (config, not rewrite).
- **Power:** the **19 V / 5 A (~95 W) barrel supply is ample** (corrected an
  earlier wrong "5 V/5 A" note — that's the USB-C/old-Nano figure). Over-current
  throttle = GPU duty cycle + power-mode, **not** the adapter; **25 W MAXN is safe**
  with this PSU + cooling.
- **Resource audit (jtop/ps):** the CERAVIS app is **~228 MB**; "used" RAM bloat
  was the **desktop + Firefox + ~20 duplicate `nvpmodel_indicator` procs + idle
  Docker** — none of which exist in production. **RAM is NOT the constraint.**
  Fixes: view the UI **off-device**, `pkill nvpmodel_indicator`, disable docker,
  go **headless** for production. Removed empty top-level `models/`, `device/`,
  `docs/` (engines live in `edge/models/`; `infra/`+`edge/` are essential;
  `settings.py`↔`jetson.env` duplication is the intended defaults+overrides
  pattern, not a bug).
- **Requirements scorecard (49 items):** ~13 done, ~6 partial, ~20 deferred
  (snapshots/clips/alert-capture tier), ~10 pending new capabilities.
- **Face + body multimodal ReID: PLANNED, DEFERRED** (user researching). Chosen:
  **InsightFace buffalo_s** (SCRFD-500m + MobileFaceNet ArcFace, ONNX→TensorRT,
  no runtime torch), **gated face-priority + weighted fusion**, separate face/
  visitor gallery so the working recipient body-ReID is untouched. (Detailed
  buffalo_s plan exists in the conversation.)

## S2.11 — Where to pick up next
1. **On-device smoke test** of everything since `v1.1-stable` (a full test plan
   was written in chat: health → stream → posture → enrollment → adaptive → zones
   → alerts → spatial → visit sessions → soak → regression). Session paused here.
2. **People model #2 — visitor identity** (body ReID; separate gallery), then
   **caregiver scheduling** (on-time/duration; demeanor scoped to activity-level,
   NOT emotion inference).
3. **Camera taxonomy + privacy + PTZ** (`*`/`&` designations, zone blur/off-limits,
   ONVIF PTZ); **head-move 25 cm** capture; **furniture-relative fall** refinement.
4. **Face + body multimodal ReID** (buffalo_s) when the user is ready.
5. **Snapshots/clips tier** (motion-duration 5/10/30 s, 3-frame summaries, 24 h
   retention + monitor save/erase) + **cloud sync** (`EventSyncer` on the existing
   `synced` flag → S3 + alerts table). User will give DB keys/endpoint for the swap.

## Net effect since v1.1-stable
ReID works end-to-end; posture is accurate + flicker-proof; the monitor is
target-focused; enrollment is guided multi-view with online-learning adaptation;
streaming auto-tunes to minimal lag per camera; and a full **alerts pipeline**
(enriched events + annotated snapshots + zone-aware fall + real-time push/ack) +
**spatial** (area transitions/inactivity) + **visit sessions** are live. ~15
feature commits on `ceravis1.1` atop `v1.1-stable`.

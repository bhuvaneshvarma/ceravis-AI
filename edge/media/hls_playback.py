from __future__ import annotations

"""
On-demand HLS-VOD playback of the recordings — the seekable "play from this
time" link.

MediaMTX's playback server only hands out a plain MP4 (no scrubbing over a long
span). To give the frontend a real timeline — pause, drag, seek — we build a
proper HLS-VOD playlist on demand: a copy-only FFmpeg (NO re-encode — the video
is already H.264, the audio already AAC) reads the recorded footage from `start`
out of MediaMTX's playback server and repackages it into HLS segments.

It's an EVENT playlist, so:
  * playback starts within ~1s (we return as soon as the first segment lands),
  * hls.js/Safari keep reloading it as it grows,
  * FFmpeg (copy is many× realtime) reaches the end of the available footage in
    seconds and writes ENDLIST — at which point the whole span is seekable.

Because recording is person-triggered, MediaMTX concatenates only the recorded
stretches (gaps skipped), so one playlist is "everything from `start` onward".

Sessions are cached by (path, start, cap) and swept after an idle TTL; each is a
folder of .ts segments + index.m3u8 under data/hls_cache, served straight back
through the same tunnel as everything else.
"""

import hashlib
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlencode

from config.settings import settings
from media.mediamtx_client import ffmpeg_available


logger = logging.getLogger("media")

_EDGE_ROOT = Path(__file__).resolve().parents[1]
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.\-]+$")


class HlsError(Exception):
    """Playlist could not be produced (no footage at that time, ffmpeg missing…)."""


class HlsPlaybackManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._procs: dict[str, subprocess.Popen] = {}   # session key -> ffmpeg

    # ---- paths -------------------------------------------------------
    def _root(self) -> Path:
        base = settings.data_path
        base = base if base.is_absolute() else (_EDGE_ROOT / base)
        root = base / "hls_cache"
        root.mkdir(parents=True, exist_ok=True)
        return root

    @staticmethod
    def _key(rec_path: str, start_iso: str, cap: int) -> str:
        return hashlib.sha1(
            f"{rec_path}|{start_iso}|{cap}".encode()).hexdigest()[:16]

    # ---- public ------------------------------------------------------
    def ensure(self, rec_path: str, start_iso: str, cap: int) -> str:
        """Return the session key for a ready-to-play playlist, generating it if
        needed. Blocks only until the FIRST segment exists (~1s), not until the
        whole span is transcoded."""
        if not ffmpeg_available():
            raise HlsError("ffmpeg not installed on this device")
        self.sweep()
        key = self._key(rec_path, start_iso, cap)
        playlist = self._root() / key / "index.m3u8"
        with self._lock:
            if playlist.exists():
                self._touch(playlist.parent)
                return key
            playlist.parent.mkdir(parents=True, exist_ok=True)
            self._spawn(key, rec_path, start_iso, cap)

        deadline = time.monotonic() + 12.0
        while time.monotonic() < deadline:
            if playlist.exists() and any(playlist.parent.glob("*.ts")):
                return key
            proc = self._procs.get(key)
            if proc is not None and proc.poll() is not None and not playlist.exists():
                raise HlsError(
                    "no footage at that time"
                    f" ({self._log_tail(playlist.parent)})")
            time.sleep(0.25)
        if playlist.exists():
            return key
        raise HlsError("timed out building the playlist")

    def file(self, key: str, filename: str) -> Path | None:
        """Resolve one session file (index.m3u8 or a .ts) for serving. Validates
        the names so a key/filename can't escape the cache dir, and keeps the
        session alive while it's being played."""
        if not (_SAFE_NAME.match(key or "") and _SAFE_NAME.match(filename or "")):
            return None
        d = self._root() / key
        f = d / filename
        if not f.is_file():
            return None
        self._touch(d)                       # played => not idle => don't sweep
        return f

    # ---- lifecycle ---------------------------------------------------
    def _spawn(self, key: str, rec_path: str, start_iso: str, cap: int) -> None:
        d = self._root() / key
        q = urlencode({"path": rec_path, "start": start_iso,
                       "duration": cap, "format": "mp4"})
        src = f"http://127.0.0.1:{settings.mediamtx_playback_port}/get?{q}"
        cmd = [
            settings.ffmpeg_binary, "-hide_banner", "-loglevel", "warning",
            "-i", src,
            "-c", "copy",                    # H.264 + AAC already — never re-encode
            "-f", "hls",
            "-hls_time", str(settings.hls_segment_secs),
            "-hls_playlist_type", "event",   # grows, then ENDLIST -> fully seekable
            "-hls_list_size", "0",           # keep every segment in the playlist
            "-hls_flags", "independent_segments",
            "-hls_segment_type", "mpegts",   # self-contained segments, no init file
            "-hls_segment_filename", str(d / "seg_%05d.ts"),
            str(d / "index.m3u8"),
        ]
        try:
            log = open(d / "ffmpeg.log", "ab")
            proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)
        except OSError as exc:
            raise HlsError(f"could not start ffmpeg: {exc}") from exc
        self._procs[key] = proc
        logger.info("HLS playback ffmpeg started: %s (pid %s)", key, proc.pid)

    def sweep(self) -> None:
        """Delete playback dirs idle past the TTL (and kill any lingering ffmpeg).
        Also reap finished procs so the map doesn't grow."""
        ttl = settings.hls_session_ttl_secs
        now = time.time()
        for key, proc in list(self._procs.items()):
            if proc.poll() is not None:
                self._procs.pop(key, None)
        try:
            dirs = list(self._root().iterdir())
        except OSError:
            return
        for d in dirs:
            if not d.is_dir():
                continue
            try:
                idle = now - d.stat().st_mtime
            except OSError:
                continue
            if idle > ttl:
                self._kill(d.name)
                shutil.rmtree(d, ignore_errors=True)

    def shutdown(self) -> None:
        for key in list(self._procs):
            self._kill(key)

    # ---- helpers -----------------------------------------------------
    def _kill(self, key: str) -> None:
        proc = self._procs.pop(key, None)
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()

    @staticmethod
    def _touch(d: Path) -> None:
        try:
            os.utime(d, None)
        except OSError:
            pass

    @staticmethod
    def _log_tail(d: Path, limit: int = 200) -> str:
        try:
            return (d / "ffmpeg.log").read_text(errors="replace")[-limit:].strip()
        except OSError:
            return "no log"


manager = HlsPlaybackManager()

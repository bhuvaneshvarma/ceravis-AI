"""
Talk-back: speaking INTO a room through the camera's own speaker.

Four small modules, one direction of audio:

    mpegts       G.711 A-law + the MPEG-TS wrapper TP-Link insists on
    protocol     the camera's local talk endpoint (port 8800, Digest, sessions)
    credentials  per-camera cloud-password HASHES on disk, never the password
    sessions     one speaker per camera, and where a camera_id resolves to

Nothing here touches ingestion, recording or live view: a talk session opens its
own short-lived socket to the camera, asks for the TALK session only (never the
preview one), and closes when the speaker stops. The media backbone keeps its
single pull per camera exactly as before.
"""

from .credentials import TalkCredential
from .sessions import TalkbackHub, camera_host, hub
from .protocol import TalkbackError, TapoTalkSession

__all__ = ["hub", "TalkbackHub", "TalkbackError", "TapoTalkSession",
           "TalkCredential", "camera_host"]

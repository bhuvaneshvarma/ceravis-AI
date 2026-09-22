"""
Talk-back: speaking INTO a room through the camera's own speaker.

Small modules, one direction of audio:

    mpegts       G.711 A-law + the MPEG-TS wrapper TP-Link insists on
    protocol     the camera's local talk endpoint (port 8800, Digest, sessions)
    credentials  the home's cloud-password HASHES on disk, never the password
    lines        the edge's own talk LINE to every camera, kept open
    sessions     the FLOOR: which carer may speak into which camera now
    audit        the talk log: who spoke into which room, when, how long
    guard        stops our retries locking a camera's account out
    advice       what to tell a person when a camera refuses the password

Nothing here touches ingestion, recording or live view: a line asks the camera
for the TALK session only (never the preview one), so the media backbone keeps
its single pull per camera exactly as before.
"""

from .credentials import TalkCredential
from .sessions import TalkbackHub, camera_host, hub
from .protocol import TalkbackError, TapoTalkSession

__all__ = ["hub", "TalkbackHub", "TalkbackError", "TapoTalkSession",
           "TalkCredential", "camera_host"]

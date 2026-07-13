"""
Local test for common.rtsp.normalize_rtsp_url — the credential encoder that fixes
the "camera password contains @" onboarding failure. No deps.

Run:  PYTHONPATH=edge python edge/tests/test_rtsp.py
"""
from common.rtsp import normalize_rtsp_url as n


CASES = [
    # (input, expected, why)
    ("rtsp://admin:Pass@123@192.168.0.250:554/stream1",
     "rtsp://admin:Pass%40123@192.168.0.250:554/stream1",
     "@ in password -> %40 (the reported break)"),
    ("rtsp://admin:Pass%40123@192.168.0.250:554/stream1",
     "rtsp://admin:Pass%40123@192.168.0.250:554/stream1",
     "already-encoded is left unchanged (idempotent)"),
    ("rtsp://192.168.0.251:554/stream2",
     "rtsp://192.168.0.251:554/stream2",
     "no credentials -> unchanged"),
    ("rtsp://admin:simple@192.168.0.9/h264",
     "rtsp://admin:simple@192.168.0.9/h264",
     "ordinary password -> unchanged"),
    ("rtsp://admin@192.168.0.9/h264",
     "rtsp://admin@192.168.0.9/h264",
     "username only -> unchanged"),
    ("rtsp://user:a@b:c@10.0.0.5:554/live",
     "rtsp://user:a%40b%3Ac@10.0.0.5:554/live",
     "@ and : in password both encoded, host untouched"),
    ("", "", "empty stays empty"),
]


def main() -> int:
    ok = True
    for src, exp, why in CASES:
        got = n(src)
        good = got == exp
        ok &= good
        print(f"  {'PASS' if good else 'FAIL'}  {why}")
        if not good:
            print(f"        in : {src}\n        exp: {exp}\n        got: {got}")
    # idempotency: normalizing twice equals normalizing once, for every case.
    for src, _exp, _why in CASES:
        if n(n(src)) != n(src):
            ok = False
            print(f"  FAIL  not idempotent: {src}")
    print("\nALL PASS" if ok else "\nSOME FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

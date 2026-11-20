"""
The app-server API key: where it comes from, and what the edge says without it.

The key (it signs a device's very first sendOtp; every call after is signed
with the device's own key, and no /v1/ai call carries it) is a secret in the root-only
/etc/ceravis/credstore/ceravis_api_key, which systemd hands ceravis.service as a
credential (the app reads it from $CREDENTIALS_DIRECTORY). A device without it must not fail silently: /api/v1/system/status lists why, and
so does a device still on the old key that was published in this repo.

Nothing here calls the app server.

Run:  PYTHONPATH=edge python tests/test_api_key.py
"""
import os
import re
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "edge"))

from config.settings import Settings, settings   # noqa: E402
from integration import ceravis_api              # noqa: E402


failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}{('  — ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


BUILT_IN = Settings.model_fields["ceravis_api_key"].default
prev_key, prev_url = settings.ceravis_api_key, settings.ceravis_api_base_url
settings.ceravis_api_base_url = "https://app.example.test/ch"


print("\n1. the key comes from the systemd credential")
creds = Path(tempfile.mkdtemp(prefix="ceravis-creds-"))
(creds / "ceravis_api_key").write_text("sk-test-from-secrets-env")
fresh = Settings(_secrets_dir=str(creds))
check("the credential file named ceravis_api_key sets the key",
      fresh.ceravis_api_key == "sk-test-from-secrets-env", fresh.ceravis_api_key[:8])


print("\n2. what the edge says about the key")
settings.ceravis_api_key = "sk-test-from-secrets-env"
check("a device's own key: no problem", ceravis_api.api_key_problem() is None)
check("...only the status beat (another address, off by default) still sends it as X-API-Key",
      ceravis_api._headers().get("X-API-Key") == "sk-test-from-secrets-env")
src = (ROOT / "edge/integration/ceravis_api.py").read_text(encoding="utf-8")
check("...no /v1/ai call builds API-key headers: _headers() serves send_status alone",
      src.count("headers=_headers()") == 1 and "_multipart_headers" not in src
      and src.index("def send_status") < src.index("headers=_headers()"))

settings.ceravis_api_key = "   "
p = ceravis_api.api_key_problem() or ""
check("no key: reported, naming the fix",
      "not installed" in p and "setup/install_api_key.sh" in p, p)
check("...and no empty X-API-Key header is sent", "X-API-Key" not in ceravis_api._headers())

check("no key is built into the code: the setting's default is empty", BUILT_IN == "",
      f"{len(BUILT_IN)} characters")
code = "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / "edge").rglob("*.py"))
check("...and no API-key literal (sk-…) is left anywhere in the edge code",
      not re.search(r"""["']sk-[A-Za-z0-9]{20,}""", code))
check("the old public key is on the revoked list, by its SHA-256 only",
      "b4dee005f91e1641f1b557b20f2945e2d593d5dcb4a24cc05a30c15f8b2989c7"
      in ceravis_api._REVOKED_API_KEYS_SHA256)
import hashlib                                   # noqa: E402
real_revoked = ceravis_api._REVOKED_API_KEYS_SHA256
ceravis_api._REVOKED_API_KEYS_SHA256 = real_revoked | {
    hashlib.sha256(b"sk-test-revoked-key").hexdigest()}
settings.ceravis_api_key = "sk-test-revoked-key"
p = ceravis_api.api_key_problem() or ""
check("a device still on a revoked key: reported, naming the fix (--replace)",
      "old revoked" in p and "setup/install_api_key.sh --replace" in p, p)
ceravis_api._REVOKED_API_KEYS_SHA256 = real_revoked

settings.ceravis_api_base_url = ""
settings.ceravis_api_key = ""
check("a standalone device (no app server) needs no key",
      ceravis_api.api_key_problem() is None)
settings.ceravis_api_base_url = "https://app.example.test/ch"


print("\n3. /api/v1/system/status lists it in problems")
try:
    from api import system_routes                # noqa: E402
except ImportError as exc:                       # pragma: no cover
    system_routes = None
    print(f"  SKIPPED: the edge web stack is not installed here ({exc}).")

if system_routes is not None:
    def status_now() -> dict:
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
        return system_routes.system_status(request)

    settings.ceravis_api_key = ""
    status = status_now()
    check("missing key -> degraded, with the reason in problems",
          status["status"] == "degraded"
          and any("API key not installed" in x for x in status["problems"]),
          "; ".join(status["problems"]))
    check("...and under cloud.api_key_problem",
          "not installed" in (status["cloud"].get("api_key_problem") or ""))
    settings.ceravis_api_key = "sk-test-from-secrets-env"
    status = status_now()
    check("a good key adds no problem",
          not any("API key" in x for x in status["problems"])
          and status["cloud"].get("api_key_problem") is None,
          "; ".join(status["problems"]))


print("\n4. the secret never sits in a tracked config file")
unit = (ROOT / "edge/infra/systemd/ceravis.service").read_text(encoding="utf-8")
env_files = re.findall(r"^EnvironmentFile=(.+)$", unit, re.M)
check("ceravis.service loads no secrets file into the environment",
      not any("secrets" in e for e in env_files), str(env_files))
jetson = (ROOT / "edge/infra/env/jetson.env").read_text(encoding="utf-8")
check("jetson.env holds no CERAVIS_API_KEY line, live or commented",
      not re.search(r"^\s*#?\s*CERAVIS_API_KEY\s*=", jetson, re.M))
installer = (ROOT / "setup/install_api_key.sh").read_text(encoding="utf-8")
check("the installer writes the credential the unit loads",
      'KEY_FILE="$CERAVIS_CREDSTORE/ceravis_api_key"' in installer)
check("...and moves a key an older setup kept in secrets.env",
      "LEGACY=/etc/ceravis/secrets.env" in installer)
for script in ("setup.sh", "upgrade.sh"):
    text = (ROOT / "setup" / script).read_text(encoding="utf-8")
    at = text.find("install_api_key.sh")
    check(f"{script} installs the key before (re)starting ceravis",
          at != -1 and at < text.find("install_service.sh")
          and (script == "setup.sh" or at < text.find("systemctl restart ceravis")))



print("\n5. replacing the key: install_api_key.sh --replace")
import shutil                                    # noqa: E402
import subprocess                                # noqa: E402


def real_bash():
    """A real bash. On Windows the `bash` on PATH is usually WSL's launcher, so take Git's."""
    if os.name != "nt":
        return shutil.which("bash") or "bash"
    git = shutil.which("git")
    for folder in Path(git).resolve().parents if git else ():
        if (folder / "bin" / "bash.exe").exists():
            return str(folder / "bin" / "bash.exe")
    return "bash"


box = Path(tempfile.mkdtemp(prefix="ceravis-keyreplace-"))
(box / "bin").mkdir()
store = box / "credstore"
store.mkdir()
(store / "ceravis_api_key").write_text("sk-test-old-key")
log = box / "sudo.log"
layout = box / "layout.env"
layout.write_text("")
# sudo runs only what touches the stand-in credstore (test, cat, sh); anything else
# (unit files, systemctl, rm, chown) is only LOGGED, so nothing on this machine changes.
(box / "bin" / "sudo").write_text(
    "#!/bin/sh\n"
    'case "$1" in test|cat|sh) exec "$@" ;; esac\n'
    f'echo "$*" >> "{log.as_posix()}"\n'
    '[ "$1" = tee ] && cat >/dev/null\n'
    "exit 0\n")
(box / "bin" / "sudo").chmod(0o755)
STUBBED = 'S="$STUBS"; command -v cygpath >/dev/null && S="$(cygpath -u "$STUBS")"; PATH="$S:$PATH"; '
key_env = {**{k: v for k, v in os.environ.items() if k != "CERAVIS_API_KEY"},
           "STUBS": str(box / "bin"), "CERAVIS_CREDSTORE": store.as_posix(),
           "CERAVIS_LAYOUT_FILE": layout.as_posix()}


def replace(stdin_text):
    return subprocess.run([real_bash(), "-c", STUBBED + "exec bash "
                           f'"{(ROOT / "setup" / "install_api_key.sh").as_posix()}" --replace'],
                          input=stdin_text, capture_output=True, text=True, env=key_env)


r = replace("sk-test-NEW-key-123\n")
stored = (store / "ceravis_api_key").read_text()
check("--replace with the key on standard input: the stored key is the new one",
      r.returncode == 0 and stored == "sk-test-NEW-key-123" and "API key saved" in r.stdout,
      (r.stdout + r.stderr)[-300:])
check("...and the key itself is never echoed", "sk-test-NEW-key-123" not in r.stdout + r.stderr)
if os.name == "posix":
    check("...root-only (0600)", (store / "ceravis_api_key").stat().st_mode & 0o777 == 0o600)
r = replace("\n")
check("--replace with nothing given: refused, the stored key unchanged",
      r.returncode == 2 and (store / "ceravis_api_key").read_text() == "sk-test-NEW-key-123",
      (r.stdout + r.stderr)[-200:])
r = replace("sk-bad key!\n")
check("--replace with a malformed key: refused, the stored key unchanged",
      r.returncode != 0 and (store / "ceravis_api_key").read_text() == "sk-test-NEW-key-123",
      (r.stdout + r.stderr)[-200:])
shutil.rmtree(box, ignore_errors=True)


settings.ceravis_api_key, settings.ceravis_api_base_url = prev_key, prev_url
if failures:
    print(f"\n{len(failures)} FAILED: " + "; ".join(failures))
    sys.exit(1)
print("\nAll API key checks passed.")

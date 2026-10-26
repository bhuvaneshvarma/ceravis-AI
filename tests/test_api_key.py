"""
The app-server API key: where it comes from, and what the edge says without it.

The key (X-API-Key on every app-server call) is a secret in the root-only
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
check("...and it is sent as X-API-Key",
      ceravis_api._headers().get("X-API-Key") == "sk-test-from-secrets-env")
check("...on multipart uploads too",
      ceravis_api._multipart_headers().get("X-API-Key") == "sk-test-from-secrets-env")

settings.ceravis_api_key = "   "
p = ceravis_api.api_key_problem() or ""
check("no key: reported, naming the fix",
      "not installed" in p and "setup/install_api_key.sh" in p, p)
check("...and no empty X-API-Key header is sent", "X-API-Key" not in ceravis_api._headers())

if BUILT_IN:
    settings.ceravis_api_key = BUILT_IN
    p = ceravis_api.api_key_problem() or ""
    check("the old built-in key: reported, naming the fix",
          "old built-in" in p and "setup/install_api_key.sh" in p, p)
else:
    check("no built-in key left in settings.py: the missing case covers it", True)

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


settings.ceravis_api_key, settings.ceravis_api_base_url = prev_key, prev_url
if failures:
    print(f"\n{len(failures)} FAILED: " + "; ".join(failures))
    sys.exit(1)
print("\nAll API key checks passed.")

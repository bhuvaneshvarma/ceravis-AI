"""`python3 -m fms_agent` runs the agent; `--check` does one exchange and reports."""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import time

from . import __version__, transport
from .agent import Agent
from .config import Config, ConfigError


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="fms_agent", description="CERAVIS fleet agent")
    ap.add_argument("--check", action="store_true", help="do one exchange now, print the result")
    ap.add_argument("--version", action="version", version=__version__)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        cfg = Config.from_env()
    except ConfigError as exc:
        print(f"fleet agent: {exc}", file=sys.stderr)
        return 2
    agent = Agent(cfg, enroll_grace=0) if args.check else Agent(cfg)   # --check never waits
    signal.signal(signal.SIGTERM, lambda *_: agent.stop.set())
    if not cfg.configured:
        # Not an error: a device without FMS_URL/FMS_ENROLL_KEY simply isn't in
        # a fleet yet. Idle quietly instead of crash-looping under systemd.
        logging.info("fleet agent idle — set FMS_URL and FMS_ENROLL_KEY to join the fleet")
        if args.check:
            return 2
        agent.stop.wait()
        return 0
    if args.check:
        return _check(agent)
    try:
        agent.run_forever()
    except KeyboardInterrupt:
        pass
    return 0


def _check(agent: Agent) -> int:
    for _ in range(3):                        # enroll → first beat → hand back any result
        try:
            nxt = agent.beat()
            if nxt > 1.0:
                print(f"OK  checking in as {agent.identity['edge_id']} with {agent.cfg.url} "
                      f"(round trip {agent.rtt_ms} ms, next in {nxt:.0f}s)")
                return 0
        except transport.ClockSkew as exc:
            agent.clock_offset = exc.server_time - time.time()
        except Exception as exc:
            print(f"FAIL {exc}", file=sys.stderr)
            return 1
    print("FAIL no settled check-in after 3 tries", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())

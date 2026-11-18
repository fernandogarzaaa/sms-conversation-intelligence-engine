"""End-to-end console demo (thin wrapper around `sms_engine.demo`).

  python scripts/demo.py            # run all scenarios A-G with decision traces
  python scripts/demo.py --seed-db  # also write ./sms_engine.db for the web UI
  python scripts/demo.py maya john  # run only named scenarios

The same commands are available as `sms-engine demo` and `sms-engine seed-db`.
Deterministic: FixedClock + seeded RNG. No network. Mock provider.
"""

from __future__ import annotations

from sms_engine.demo import main

if __name__ == "__main__":
    raise SystemExit(main())

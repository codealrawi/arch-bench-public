"""HEALTHCHECK контейнера: GET http://127.0.0.1:$APP_PORT$HEALTH_PATH → код выхода 0/1."""

import os
import sys
import urllib.request

port = os.environ.get("APP_PORT", "8000")
path = os.environ.get("HEALTH_PATH", "/health")
try:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=2) as r:  # noqa: S310
        sys.exit(0 if r.status == 200 else 1)
except Exception:  # noqa: BLE001
    sys.exit(1)

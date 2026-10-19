"""Точка входа эксперимента (структура задания): делегирует experiments.orchestrator.run."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from experiments.orchestrator.run import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())

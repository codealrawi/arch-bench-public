"""Запуск одной ступени k6 — локально или на отдельной машине генератора по SSH (P-04).

Генератор задаётся переменной GENERATOR_SSH (``user@host``); рабочий каталог на нём —
GENERATOR_WORKDIR. Без GENERATOR_SSH k6 запускается локально (допустимо только в
local-validation: в окружении server оркестратор это запрещает).
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path

from experiments.config import K6_DIR, REPO_ROOT, BenchConfig
from experiments.orchestrator.shell import Shell

SCENARIO = K6_DIR / "scenario.js"
TEXTS = K6_DIR / "write_texts.json"
CPU_SAMPLER = REPO_ROOT / "scripts" / "cpu_sampler.py"
# Ubuntu по умолчанию даёт 1024 дескриптора: при 5000 VU (соединение на VU) k6 упирается
# в лимит, и «too many open files» засчитывались бы архитектуре как транспортные ошибки (M-4).
K6_NOFILE = 65536


def k6_seed(cfg: BenchConfig, run_index: int, step_index: int) -> int:
    """P-29: одинаков у всех архитектур для одной пары (прогон, ступень)."""
    return cfg.load.k6_seed_base + 1000 * run_index + step_index


def k6_env(
    cfg: BenchConfig, *, architecture: str, base_url: str, vus: int, seed: int, texts_path: str
) -> dict[str, str]:
    from database.seed.generator import user_email

    ld = cfg.load
    routes = cfg.architectures[architecture].entry.route_prefixes
    return {
        "TARGET_BASE": base_url,
        "ROUTES_JSON": json.dumps(routes, sort_keys=True),
        "MIX_JSON": json.dumps(ld.mix, sort_keys=True),
        "SEARCH_TERMS_JSON": json.dumps(ld.search_terms, ensure_ascii=False),
        "USERS": str(cfg.data.users),
        "POST_POOL_PAGES": str(ld.post_pool_pages),
        "EMAIL_TEMPLATE": user_email(0).replace("0@", "{user_id}@", 1),
        "VUS": str(vus),
        "RAMP_S": str(ld.ramp_s),
        "WARMUP_S": str(ld.warmup_s),
        "MEASURE_S": str(ld.measure_s),
        "THINK_S": str(ld.think_time_s),
        "TIMEOUT_S": str(ld.timeout_s),
        "K6_SEED": str(seed),
        "LOGIN_POOL": str(min(ld.login_pool_size, cfg.data.users)),
        "PASSWORD_TEMPLATE": cfg.auth.password_template,
        "TEXTS_FILE": texts_path,
    }


@dataclass
class K6Result:
    exit_code: int
    points: Path
    summary: Path
    generator_cpu: Path
    log: Path


class K6Runner:
    def __init__(self, cfg: BenchConfig, shell: Shell):
        self.cfg, self.sh = cfg, shell
        self.remote = os.environ.get("GENERATOR_SSH")
        self.remote_dir = os.environ.get("GENERATOR_WORKDIR", "arch-bench-gen")

    @property
    def is_remote(self) -> bool:
        return bool(self.remote)

    def _duration(self) -> float:
        ld = self.cfg.load
        return ld.ramp_s + ld.warmup_s + ld.measure_s + ld.timeout_s + 60

    def prepare(self) -> None:
        if not self.remote:
            return
        self.sh.run(["ssh", self.remote, f"mkdir -p {shlex.quote(self.remote_dir)}"])
        self.sh.run(
            [
                "scp",
                str(SCENARIO),
                str(TEXTS),
                str(CPU_SAMPLER),
                f"{self.remote}:{self.remote_dir}/",
            ]
        )

    def run_step(self, step_dir: Path, env: dict[str, str]) -> K6Result:
        step_dir.mkdir(parents=True, exist_ok=True)
        res = K6Result(
            1,
            step_dir / "points.json.gz",
            step_dir / "k6_summary.json",
            step_dir / "generator_cpu.csv",
            step_dir / "k6.log",
        )
        interval = "1"
        if not self.remote:
            env_local = {**os.environ, **env, "TEXTS_FILE": str(TEXTS)}
            sampler = self.sh.popen(
                [
                    sys.executable,
                    str(CPU_SAMPLER),
                    "--out",
                    str(res.generator_cpu),
                    "--interval",
                    interval,
                    "--duration",
                    str(self._duration()),
                ]
            )
            out = self.sh.run(
                [
                    "k6",
                    "run",
                    "--quiet",
                    "--out",
                    f"json={res.points}",
                    "--summary-export",
                    str(res.summary),
                    str(SCENARIO),
                ],
                env=env_local,
                check=False,
                timeout=self._duration() + 900,
            )
            res.log.write_text(out, encoding="utf-8")
            if sampler is not None:
                sampler.terminate()
            res.exit_code = 0 if self.sh.dry_run or res.summary.exists() else 1
            return res
        rd = self.remote_dir
        env_r = {**env, "TEXTS_FILE": "write_texts.json"}
        assign = " ".join(f"{k}={shlex.quote(v)}" for k, v in sorted(env_r.items()))
        cmd = (
            f"cd {shlex.quote(rd)} && rm -f points.json.gz k6_summary.json generator_cpu.csv"
            f" && (python3 cpu_sampler.py --out generator_cpu.csv --interval {interval}"
            f" --duration {self._duration():.0f} &) && ulimit -n {K6_NOFILE}"
            f" && {assign} k6 run --quiet"
            f" --out json=points.json.gz --summary-export k6_summary.json scenario.js;"
            f" echo K6_EXIT=$?"
        )
        out = self.sh.run(["ssh", self.remote, cmd], check=False, timeout=self._duration() + 900)
        res.log.write_text(out, encoding="utf-8")
        for name in ("points.json.gz", "k6_summary.json", "generator_cpu.csv"):
            self.sh.run(["scp", f"{self.remote}:{rd}/{name}", str(step_dir / name)], check=False)
        res.exit_code = 0 if "K6_EXIT=0" in out or self.sh.dry_run else 1
        return res

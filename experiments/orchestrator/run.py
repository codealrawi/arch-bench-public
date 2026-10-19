"""Главный цикл эксперимента (протокол §6–§8, §12).

    python -m experiments.orchestrator.run --environment server --target-host 10.0.0.5 \
        --prometheus-url http://127.0.0.1:9090
    python -m experiments.orchestrator.run --environment local-validation --target-host 127.0.0.1 \
        --architectures monolith --runs 1

Для каждого номера прогона r: конфигурации по очереди (с циклическим сдвигом порядка, P-40);
для каждой конфигурации: teardown → deploy → seed → (для каждой ступени: сброс БД → состояние
до → k6 → состояние после → мониторинг → проверка невалидности) → teardown.
Существующие каталоги прогонов не перезаписываются.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import shutil
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import asyncpg

from apps.shared.moderation import ContentModerator
from database.seed import snapshot
from database.seed.generator import generate_load_texts
from experiments import meta as host_meta
from experiments.config import (
    ARCHITECTURES,
    DEFAULT_CONFIG_PATH,
    RAW_DIR,
    REPO_ROOT,
    BenchConfig,
    SlaMissingError,
    config_sha256,
    load_bench_config,
    resolve_sla_ms,
)
from experiments.orchestrator import invalid, resources
from experiments.orchestrator.deployers import Deployer, make_deployer
from experiments.orchestrator.k6runner import TEXTS, K6Runner, k6_env, k6_seed
from experiments.orchestrator.samplers import PgConnectionsSampler, PodsSampler
from experiments.orchestrator.shell import Shell
from infrastructure import render

log = logging.getLogger("arch-bench.run")
ENVIRONMENTS = ("local-validation", "server")
K8S_NAMESPACES = {
    "microservices": ["arch-bench"],
    "serverless": ["arch-bench", "openfaas", "openfaas-fn"],
}


class RunAborted(RuntimeError):
    pass


def interleaved_order(archs: list[str], run_index: int) -> list[str]:
    """P-40: в цикле r порядок конфигураций циклически сдвинут на (r−1)."""
    k = (run_index - 1) % len(archs)
    return archs[k:] + archs[:k]


async def _db(dsn: str, cfg: BenchConfig, action: str) -> dict[str, Any]:
    """load — генерация эталона (после развёртывания); reset — сброс + seed перед ступенью
    (рабочие таблицы := детерминированный эталон, P-19); state — состояние после ступени."""
    window = cfg.data.stats_window_days
    conn = await asyncpg.connect(dsn)
    try:
        if action == "load":
            timings = await snapshot.load_snapshot(
                conn, snapshot.params_from_config(cfg), ContentModerator().train()
            )
            await snapshot.reset(conn)
            return {
                **timings,
                "postgres_server_version": await conn.fetchval("SHOW server_version"),
                "migration": await conn.fetchval("SELECT version_num FROM alembic_version"),
                "state": (await snapshot.verify(conn, window)).to_dict(),
            }
        if action == "reset":
            seconds = await snapshot.reset(conn)
            return {"reset_s": round(seconds, 2), **(await snapshot.verify(conn, window)).to_dict()}
        return (await snapshot.data_state(conn, window)).to_dict()
    finally:
        await conn.close()


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Experiment:
    def __init__(self, cfg: BenchConfig, args: argparse.Namespace, shell: Shell):
        self.cfg, self.args, self.sh = cfg, args, shell
        self.results = Path(args.results)
        self.build = render.GENERATED
        self.k6 = K6Runner(cfg, shell)
        self.sla_ms = resolve_sla_ms(cfg)
        self.steps = cfg.load.steps_server if args.environment == "server" else cfg.load.steps_local

    # ── предварительные проверки ────────────────────────────────────────────────────────
    def preflight(self) -> dict[str, Any]:
        host = host_meta.collect(self.args.target_host, None)
        if self.args.environment == "server" and not self.sh.dry_run:
            g = host["git"]
            if not g.get("commit") or g.get("dirty"):
                raise RunAborted("server: нужен git commit без незакоммиченных изменений (P-13)")
            if not self.k6.is_remote:
                raise RunAborted("server: k6 должен работать на отдельной машине (GENERATOR_SSH)")
            if not self.args.prometheus_url:
                raise RunAborted("server: нужен --prometheus-url (мониторинг P-49)")
        stale = render.stale(self.cfg)
        if stale:
            if self.args.environment == "server" and not self.sh.dry_run:
                # развёртывание должно соответствовать закоммиченному коду (commit в meta.json)
                raise RunAborted(
                    "infrastructure/generated устарел относительно bench.yaml: "
                    "выполните python -m infrastructure.render и закоммитьте"
                )
            render.write(self.cfg)
        if not TEXTS.is_file():
            d = self.cfg.data
            texts = generate_load_texts(
                d.seed,
                2000,
                d.suspicious_share,
                (d.body_words_min, d.body_words_max),
                (d.comment_words_min, d.comment_words_max),
            )
            TEXTS.write_text(json.dumps(texts, ensure_ascii=False), encoding="utf-8")
        self.k6.prepare()
        return host

    def generator_meta(self) -> dict[str, Any]:
        if not self.k6.is_remote:
            return {"same_host_as_target": True, "note": "local-validation: k6 на этой машине"}
        remote, rd = self.k6.remote, self.k6.remote_dir
        self.sh.run(["scp", str(REPO_ROOT / "experiments" / "meta.py"), f"{remote}:{rd}/meta.py"])
        port = getattr(self.cfg.network, self.cfg.architectures["monolith"].entry.port_key)
        out = self.sh.run(
            [
                "ssh",
                str(remote),
                f"cd {rd} && python3 meta.py --standalone "
                f"--rtt-host {self.args.target_host} --rtt-port {port}",
            ],
            check=False,
        )
        try:
            return dict(json.loads(out)) if out else {"error": "пустой ответ"}
        except json.JSONDecodeError:
            return {"error": "не удалось разобрать метаданные генератора", "raw": out[:500]}

    # ── один прогон ─────────────────────────────────────────────────────────────────────
    def run_one(self, arch: str, run_index: int, host: dict[str, Any]) -> bool:
        run_dir = self.results / arch / f"run_{run_index:02d}"
        if run_dir.exists():
            raise RunAborted(f"{run_dir} уже существует — перезапись запрещена")
        run_dir.mkdir(parents=True)
        fh = logging.FileHandler(run_dir / "run.log", encoding="utf-8")
        logging.getLogger().addHandler(fh)
        dep = make_deployer(arch, self.cfg, self.build, self.sh, self.args.target_host)
        meta: dict[str, Any] = {
            "architecture": arch,
            "run_index": run_index,
            "environment": self.args.environment,
            "protocol_version": self.cfg.protocol_version,
            "config_sha256": config_sha256(Path(self.args.config)),
            "config": self.cfg.model_dump(),
            "sla_ms": self.sla_ms,
            "steps": self.steps,
            "started_at_utc": datetime.now(UTC).isoformat(),
            "host": host,
            "generator": self.generator_meta(),
            "resources": json.loads((self.build / "resources.json").read_text(encoding="utf-8"))[
                "architectures"
            ][arch],
        }
        reasons: list[str] = []
        try:
            dep.down()
            dep.up()
            dep.wait_ready()
            dsn = dep.db_dsn()
            meta["db_seed"] = {} if self.sh.dry_run else asyncio.run(_db(dsn, self.cfg, "load"))
            for step_index, vus in enumerate(self.steps):
                reasons = self.run_step(dep, arch, run_dir, run_index, step_index, vus, dsn)
                if reasons:
                    break
        except Exception as exc:  # сбой стенда: прогон невалиден, но данные сохраняются
            log.exception("сбой прогона")
            reasons = [f"сбой стенда: {type(exc).__name__}: {exc}"]
        finally:
            try:
                dep.down()
            except Exception as exc:  # noqa: BLE001
                reasons.append(f"teardown: {exc}")
            meta["finished_at_utc"] = datetime.now(UTC).isoformat()
            meta["invalid_reasons"] = reasons
            meta["valid"] = not reasons
            (run_dir / "meta.json").write_text(
                json.dumps(meta, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
            )
            logging.getLogger().removeHandler(fh)
            fh.close()
        if reasons:
            self._move_invalid(run_dir, reasons)
            return False
        self._append_manifest(run_dir)
        return True

    def run_step(
        self,
        dep: Deployer,
        arch: str,
        run_dir: Path,
        run_index: int,
        step_index: int,
        vus: int,
        dsn: str,
    ) -> list[str]:
        step_dir = run_dir / f"step_{vus:04d}"
        step_dir.mkdir()
        smeta: dict[str, Any] = {
            "vus": vus,
            "step_index": step_index,
            "k6_seed": k6_seed(self.cfg, run_index, step_index),
        }
        if not self.sh.dry_run:
            smeta["db_state_before"] = asyncio.run(_db(dsn, self.cfg, "reset"))
        samplers: list[Any] = []
        monitored: list[str] = []
        if not self.sh.dry_run:
            samplers.append(
                PgConnectionsSampler(
                    step_dir / "pg_connections.csv", dsn, self.cfg.metrics.monitoring_step_s
                )
            )
            monitored.append("pg_connections.csv")
            if arch in K8S_NAMESPACES:
                samplers.append(
                    PodsSampler(
                        step_dir / "pods.csv",
                        K8S_NAMESPACES[arch],
                        self.cfg.metrics.monitoring_step_s,
                    )
                )
                monitored.append("pods.csv")
            for s in samplers:
                s.start()
        smeta["k6_started_at"] = time.time()
        env = k6_env(
            self.cfg,
            architecture=arch,
            base_url=dep.entry_base(),
            vus=vus,
            seed=smeta["k6_seed"],
            texts_path=str(TEXTS),
        )
        res = self.k6.run_step(step_dir, env)
        smeta["k6_finished_at"] = time.time()
        smeta["k6_exit_code"] = res.exit_code
        for s in samplers:
            s.stop()
        smeta["sampler_errors"] = [e for s in samplers for e in s.errors]
        if self.sh.dry_run:
            (step_dir / "step_meta.json").write_text(json.dumps(smeta), encoding="utf-8")
            return []
        smeta["db_state_after"] = asyncio.run(_db(dsn, self.cfg, "state"))
        if self.args.prometheus_url:
            ld = self.cfg.load
            t_end = smeta["k6_finished_at"]
            try:
                resources.export(
                    self.args.prometheus_url,
                    arch,
                    t_end - ld.timeout_s,
                    ld.measure_s,
                    step_dir / "resources.csv",
                )
            except Exception as exc:  # noqa: BLE001
                smeta["resources_error"] = str(exc)
        (step_dir / "step_meta.json").write_text(
            json.dumps(smeta, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        reasons = invalid.evaluate_step(step_dir, self.cfg, res.exit_code, monitored)
        if self.args.environment == "server":
            if not (step_dir / "resources.csv").is_file():
                reasons.append("нет resources.csv (CPU по контейнерам не измерен)")
            elif "resources_error" in smeta:
                reasons.append(f"учёт ресурсов неполон: {smeta['resources_error']}")
        return reasons

    def _move_invalid(self, run_dir: Path, reasons: list[str]) -> None:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        dest = run_dir.parent / "_invalid" / f"{run_dir.name}_{stamp}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(run_dir), str(dest))
        (dest / "INVALID_REASON.md").write_text(
            "# Невалидный прогон (протокол §12.2)\n\n"
            + "\n".join(f"- {r}" for r in reasons)
            + "\n\nСырые данные сохранены; прогон повторяется с тем же номером.\n",
            encoding="utf-8",
        )
        log.warning("прогон %s невалиден: %s", run_dir, reasons)

    def _append_manifest(self, run_dir: Path) -> None:
        lines = [
            f"{_sha(p)}  {p.relative_to(self.results).as_posix()}"
            for p in sorted(run_dir.rglob("*"))
            if p.is_file() and p.name != "run.log"
        ]
        with (self.results / "MANIFEST.sha256").open("a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

    # ── вся серия ───────────────────────────────────────────────────────────────────────
    def run(self) -> int:
        host = self.preflight()
        archs = [a for a in ARCHITECTURES if a in self.args.architectures]
        first = self.args.start_run
        runs = self.args.runs or self.cfg.runs.count
        for r in range(first, first + runs):
            order = interleaved_order(archs, r) if self.cfg.runs.interleave else archs
            for arch in order:
                for attempt in range(1, self.args.max_attempts + 1):
                    log.info("=== %s run_%02d (попытка %d) ===", arch, r, attempt)
                    if self.run_one(arch, r, host):
                        break
                else:
                    log.error(
                        "%s run_%02d: все %d попыток невалидны — серия остановлена",
                        arch,
                        r,
                        self.args.max_attempts,
                    )
                    return 3
        return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(prog="experiments.orchestrator.run")
    ap.add_argument("--environment", required=True, choices=ENVIRONMENTS)
    ap.add_argument("--target-host", required=True)
    ap.add_argument(
        "--architectures", nargs="+", default=list(ARCHITECTURES), choices=list(ARCHITECTURES)
    )
    ap.add_argument("--runs", type=int, default=None)
    ap.add_argument("--start-run", type=int, default=1)
    ap.add_argument("--max-attempts", type=int, default=3)
    ap.add_argument("--prometheus-url")
    ap.add_argument("--results", default=str(RAW_DIR))
    ap.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    ap.add_argument("--dry-run", action="store_true", help="только напечатать команды")
    args = ap.parse_args(argv)
    try:
        cfg = load_bench_config(args.config)
        exp = Experiment(cfg, args, Shell(dry_run=args.dry_run))
        return exp.run()
    except SlaMissingError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except RunAborted as exc:
        print(f"прервано: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

from __future__ import annotations

import json
import logging
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from queue import Empty, Queue

from .bms import Catalogue, CinemaCollector
from .config import generation, load_app
from .core import IST, Observation, now, resolve_date
from .delivery import deliver
from .http import HttpClient
from .ports import NotificationSender, ProviderCollector
from .providers import Collector
from .readiness import ReadinessGate
from .state import State

logger = logging.getLogger(__name__)


@dataclass
class Job:
    watch: dict
    key: str
    provider: str
    collector: ProviderCollector
    cinema: CinemaCollector | None = None
    due: float = 0
    failures: int = 0
    future: object = None
    batch: int = 0
    summaries: list = field(default_factory=list)

    def collect(self, stop, emit=None):
        today = now().astimezone(IST).date().isoformat()
        days = sorted({resolve_date(day) for day in self.watch["dates"]} - {""})
        days = [day for day in days if day >= today]
        if self.cinema:
            return self.cinema.collect_dates(days, emit=emit) if days else []
        results = []
        for day in days:
            if stop.is_set():
                break
            obs = self.collector.collect(self.provider, day)
            obs.observed_at = now().isoformat()
            results.append(obs)
            if emit:
                emit(obs)
        return results


def run(
    config_path,
    db_path,
    *,
    loop=False,
    cycles=0,
    sender: NotificationSender | None = None,
    targets=(),
    report_path=None,
):
    app = load_app(config_path)
    state = State(db_path)
    root = Path(db_path).parent
    catalogue = Catalogue(
        str(root / "catalogue"), app["catalogue_refresh_seconds"], app["max_backoff_seconds"]
    )
    stop = threading.Event()
    wake = threading.Event()
    old_signals = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        old_signals[sig] = signal.signal(sig, lambda *_: (stop.set(), wake.set()))
    executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="movie-provider")
    jobs = {}
    retired = []
    health = {}
    scope_errors = {}
    last_sent = {}
    events = Queue()
    config_signature = None
    config_error = None
    started_at = now().isoformat()
    reload_at = 0
    prune_at = 0
    completed = {}
    last_report_signature = None
    report_due = 0
    report_path = Path(report_path) if report_path else root / "latest.json"
    readiness = ReadinessGate(app, wake=wake) if sender else None

    def emit(job, batch, obs):
        events.put((job, batch, obs))
        wake.set()

    def accept(job, obs):
        try:
            accepted = state.ingest(
                job.key, job.watch, obs, app["theatre_aliases"], freshness=app["freshness_seconds"]
            )
        except (ValueError, KeyError, TypeError) as exc:
            obs.complete, obs.status, obs.error = False, "REJECTED", str(exc)
            accepted = state.ingest(
                job.key, job.watch, obs, app["theatre_aliases"], freshness=app["freshness_seconds"]
            )
        if obs.error:
            accepted["error"] = obs.error
        scope_key = (job.key, accepted["scope"])
        reason = (
            accepted.get("error") or "; ".join(accepted.get("errors", [])) or accepted["status"]
        )
        if not accepted["complete"]:
            if scope_errors.get(scope_key) != reason:
                logger.warning(
                    "Scope rejected: watch=%s provider=%s scope=%s reason=%s",
                    job.watch["id"],
                    job.provider,
                    accepted["scope"],
                    reason,
                )
            scope_errors[scope_key] = reason
        elif scope_key in scope_errors:
            logger.info(
                "Scope recovered: watch=%s provider=%s scope=%s",
                job.watch["id"],
                job.provider,
                accepted["scope"],
            )
            del scope_errors[scope_key]
        job.summaries.append(accepted)
        if not accepted["complete"] and "|".join((job.key, job.provider)) in health:
            health["|".join((job.key, job.provider))]["state"] = "DEGRADED"

    def drain():
        while True:
            try:
                job, batch, obs = events.get_nowait()
            except Empty:
                break
            if jobs.get((job.key, job.provider)) is job and job.batch == batch:
                accept(job, obs)

    def sync_jobs():
        desired = {}
        for watch in app["watches"]:
            if not watch.get("enabled", True):
                continue
            key = generation(watch)
            for provider in watch["providers"]:
                job_key = (key, provider)
                if job_key in jobs:
                    job = jobs[job_key]
                    job.watch = watch
                    if job.cinema:
                        job.cinema.background_checks = app["bms_background_checks"]
                        job.cinema.probe_interval = app["bms_probe_interval_seconds"]
                        job.cinema.active.interval = watch["poll_seconds"]
                        job.cinema.active.maximum = app["max_backoff_seconds"]
                        job.cinema.probes.maximum = app["max_backoff_seconds"]
                else:
                    collector = Collector(watch, client=HttpClient())
                    cinema = (
                        CinemaCollector(
                            watch,
                            collector.client,
                            catalogue,
                            app["bms_background_checks"],
                            stop,
                            app["bms_probe_interval_seconds"],
                            app["max_backoff_seconds"],
                        )
                        if provider == "bookmyshow"
                        else None
                    )
                    job = Job(watch, key, provider, collector, cinema)
                desired[job_key] = job
        for key, job in jobs.items():
            if key not in desired:
                retired.append(job)
        jobs.clear()
        jobs.update(desired)
        active_health = {"|".join(key) for key in desired}
        for key in list(health):
            if key not in active_health:
                del health[key]
        for key in list(completed):
            if key not in desired:
                del completed[key]
        active_watches = {key for key, _ in desired}
        state.reconcile_aliases(active_watches, app["theatre_aliases"])
        for key in list(scope_errors):
            if key[0] not in active_watches:
                del scope_errors[key]

    try:
        sync_jobs()
        logger.info(
            "Loaded %s movie watches; telegram=%s; existing listings are silently baselined",
            len(app["watches"]),
            bool(sender),
        )
        while not stop.is_set():
            tick = time.monotonic()
            if tick >= reload_at:
                reload_at = tick + 5
                try:
                    stat = Path(config_path).stat()
                    signature = (stat.st_mtime_ns, stat.st_size)
                    if signature != config_signature:
                        updated = load_app(config_path)
                        app = updated
                        if readiness:
                            readiness.app = app
                            readiness.prober.timeout = app["readiness_timeout_seconds"]
                        catalogue.refresh_seconds = app["catalogue_refresh_seconds"]
                        catalogue.schedule.interval = app["catalogue_refresh_seconds"]
                        catalogue.schedule.maximum = app["max_backoff_seconds"]
                        sync_jobs()
                        config_signature = signature
                        if config_error:
                            logger.info(
                                "Preference configuration recovered; notifications may resume"
                            )
                        config_error = None
                except (ValueError, OSError, KeyError, TypeError) as exc:
                    error = f"{type(exc).__name__}: {exc}"
                    if error != config_error:
                        logger.error(
                            "Invalid preference configuration; notifications paused: %s", error
                        )
                    config_error = error
            for job_key, job in list(jobs.items()):
                drain()
                if job.future and job.future.done():
                    try:
                        job.future.result()
                        drain()  # All observations were queued before the future completed.
                    except Exception as exc:
                        state.invalidate_provider(job.key, job.provider)
                        accept(
                            job,
                            Observation(
                                job.provider,
                                now().astimezone(IST).date().isoformat(),
                                status="ERROR",
                                complete=False,
                                error=f"Collector failed: {type(exc).__name__}",
                            ),
                        )
                    job.future = None
                    summary = job.summaries
                    good = all(s["complete"] for s in summary)
                    active_days = any(
                        resolve_date(d) >= now().astimezone(IST).date().isoformat()
                        for d in job.watch["dates"]
                    )
                    components = job.cinema.components() if job.cinema and active_days else None
                    if components:
                        good = all(
                            components[k]["state"] in ("HEALTHY", "REFRESHING")
                            for k in ("catalogue", "cinemas", "discovery_probes")
                        )
                    job.failures = 0 if good else job.failures + 1
                    base = job.watch["poll_seconds"]
                    interval = min(
                        base * 2 ** min(job.failures, 4), max(base, app["max_backoff_seconds"])
                    )
                    if job.cinema:
                        interval = (
                            5 if active_days else base
                        )  # Individual component clocks own their retries.
                    job.due = time.monotonic() + interval
                    completed[job_key] = completed.get(job_key, 0) + 1
                    health["|".join(job_key)] = {
                        "watch": job.watch["id"],
                        "movie": job.watch["movie"],
                        "provider": job.provider,
                        "checked_at": now().isoformat(),
                        "state": ("HEALTHY" if good else "DEGRADED")
                        if active_days
                        else "IDLE_DATES_EXPIRED",
                        "scopes": summary,
                        "next_poll_seconds": interval,
                        "components": components,
                    }
                    if not job.cinema or summary:
                        logger.info(
                            "Provider scan: watch=%s provider=%s state=%s updated_scopes=%s next_check_seconds=%s",
                            job.watch["id"],
                            job.provider,
                            "healthy"
                            if good and active_days
                            else "degraded"
                            if active_days
                            else "idle",
                            len(summary),
                            interval,
                        )
                if job.future is None and tick >= job.due and (loop or not completed.get(job_key)):
                    job.batch += 1
                    job.summaries = []
                    batch = job.batch
                    job.future = executor.submit(
                        job.collect,
                        stop,
                        lambda obs, job=job, batch=batch: emit(job, batch, obs),
                    )
            drain()
            for job in list(retired):
                if not job.future or job.future.done():
                    job.collector.close()
                    retired.remove(job)
            active = {job.key for job in jobs.values()}
            delivery = (
                deliver(
                    state,
                    sender,
                    active,
                    targets,
                    app,
                    readiness=readiness,
                    last_sent=last_sent,
                    stop=stop,
                )
                if sender and not config_error
                else None
            )
            if tick >= prune_at:
                state.prune(app["observation_retention_days"])
                prune_at = tick + 3600
            report = {
                "checked_at": now().isoformat(),
                "started_at": started_at,
                "running": True,
                "telegram_enabled": bool(sender),
                "config_error": config_error,
                "watches": [w["id"] for w in app["watches"] if w.get("enabled", True)],
                "providers": list(health.values()),
                "state": state.status(),
                "delivery": delivery,
                "readiness": readiness.summary() if readiness else None,
                "semantics": "Initial successful scopes are silent baselines. First detected times are local observations.",
            }
            report["coverage"] = (
                "DEGRADED"
                if config_error or any(h["state"] == "DEGRADED" for h in health.values())
                else "INITIALIZING"
                if len(health) < len(jobs)
                else "HEALTHY"
            )
            report["needs_delivery_review"] = any(
                report["state"]["delivery_states"].get(s, 0) for s in ("UNKNOWN", "FAILED")
            )
            report_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = report_path.with_suffix(".json.tmp")
            signature = json.dumps(
                {k: v for k, v in report.items() if k not in ("checked_at", "delivery")},
                sort_keys=True,
            )
            if signature != last_report_signature or tick >= report_due:
                temporary.write_text(
                    json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
                )
                temporary.replace(report_path)
                report_due, last_report_signature = tick + 30, signature
            finished = jobs and all(completed.get(key, 0) >= (cycles or 1) for key in jobs)
            if (not loop or cycles) and (finished or not jobs):
                if readiness is None or not readiness.inflight:
                    break
            wake.wait(0.25)
            wake.clear()
    finally:
        stop.set()
        executor.shutdown(wait=True, cancel_futures=True)
        if readiness:
            readiness.close()
        catalogue.close()
        for job in [*jobs.values(), *retired]:
            job.collector.close()
        if "report" in locals():
            report.update(running=False, stopped_at=now().isoformat(), state=state.status())
            temporary = report_path.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
            temporary.replace(report_path)
        state.close()
        for sig, handler in old_signals.items():
            signal.signal(sig, handler)
    return report

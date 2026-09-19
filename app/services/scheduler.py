import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from app.database import SessionLocal, Settings, get_settings
from app.models import DiscoveryRun, DiscoveryRunStatus
from app.schemas import SearchCriteria
from app.services.discovery import search_and_persist

from app.scoring import RelevanceCategory

logger = logging.getLogger("app.scheduler")


class DiscoverySchedulerManager:
    def __init__(self):
        self._lock = asyncio.Lock()
        self._bg_task: Optional[asyncio.Task] = None
        self._is_running = False

    def start(self, settings: Optional[Settings] = None):
        if settings is None:
            settings = get_settings()
        if not settings.discovery_scheduler_enabled:
            logger.info("Discovery Scheduler is disabled in settings.")
            return

        if self._is_running:
            logger.warning("Discovery Scheduler is already running.")
            return

        self._is_running = True
        self._bg_task = asyncio.create_task(self._scheduler_loop())
        logger.info(f"Discovery Scheduler started (interval: {settings.discovery_scheduler_interval_hours}h).")

    def stop(self):
        self._is_running = False
        if self._bg_task:
            self._bg_task.cancel()
            self._bg_task = None
        logger.info("Discovery Scheduler stopped.")

    async def _scheduler_loop(self):
        settings = get_settings()
        if settings.discovery_run_on_startup:
            logger.info("Executing startup discovery run...")
            await self.trigger_manual_run(trigger_source="STARTUP")

        while self._is_running:
            try:
                interval_seconds = settings.discovery_scheduler_interval_hours * 3600
                await asyncio.sleep(interval_seconds)
                if not self._is_running:
                    break
                logger.info("Executing scheduled discovery run...")
                await self.trigger_manual_run(trigger_source="SCHEDULED")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in scheduler loop: {e}", exc_info=True)
                await asyncio.sleep(60)

    async def trigger_manual_run(
        self,
        criteria: Optional[SearchCriteria] = None,
        trigger_source: str = "MANUAL",
        settings: Optional[Settings] = None,
    ) -> DiscoveryRun:
        if settings is None:
            settings = get_settings()

        if criteria is None:
            criteria = SearchCriteria()

        db = SessionLocal()
        run_uuid = f"run_{uuid.uuid4().hex[:12]}"
        now_dt = datetime.now(timezone.utc).replace(tzinfo=None)

        try:
            # Concurrency check
            if self._lock.locked():
                logger.warning("Discovery run skipped: Another run is currently in progress.")
                run_rec = DiscoveryRun(
                    run_id=run_uuid,
                    status=DiscoveryRunStatus.skipped,
                    started_at=now_dt,
                    finished_at=now_dt,
                    errors_json={"error": "Another discovery run is already in progress.", "trigger_source": trigger_source},
                )
                db.add(run_rec)
                db.commit()
                db.refresh(run_rec)
                return run_rec

            async with self._lock:
                run_rec = DiscoveryRun(
                    run_id=run_uuid,
                    status=DiscoveryRunStatus.running,
                    started_at=now_dt,
                    errors_json={"trigger_source": trigger_source},
                )
                db.add(run_rec)
                db.commit()
                db.refresh(run_rec)

                try:
                    max_runtime_seconds = settings.discovery_scheduler_max_runtime_minutes * 60
                    result = await asyncio.wait_for(
                        search_and_persist(db, criteria, settings),
                        timeout=max_runtime_seconds,
                    )

                    ended_at = datetime.now(timezone.utc).replace(tzinfo=None)

                    # Compute statistics: explicit PFE/internship/graduate intent according to RelevanceCategory
                    high_value_count = sum(
                        1
                        for app in result.applications
                        if getattr(app, "relevance_category", None)
                        in [
                            RelevanceCategory.EXPLICIT_PFE.value,
                            RelevanceCategory.EXPLICIT_INTERNSHIP.value,
                            RelevanceCategory.GRADUATE.value,
                        ]
                    )

                    sources_attempted = len(result.sources_queried)
                    sources_failed = len(result.errors_by_provider)
                    sources_succeeded = max(0, sources_attempted - sources_failed)

                    prov_metrics = []
                    has_errors = bool(result.errors_by_provider)

                    for pstat in result.provider_stats:
                        prov_metrics.append({
                            "provider_name": pstat.provider_name,
                            "status": pstat.status,
                            "jobs_fetched": pstat.jobs_fetched,
                            "jobs_normalized": pstat.jobs_normalized,
                            "new_jobs": pstat.new_jobs,
                            "duplicates": pstat.duplicates,
                            "low_score_jobs": pstat.low_score_jobs,
                            "errors_or_restrictions": pstat.errors_or_restrictions,
                        })

                    run_rec.status = DiscoveryRunStatus.partial if has_errors else DiscoveryRunStatus.completed
                    run_rec.finished_at = ended_at
                    run_rec.sources_attempted = sources_attempted
                    run_rec.sources_succeeded = sources_succeeded
                    run_rec.sources_failed = sources_failed
                    run_rec.jobs_fetched = result.jobs_fetched
                    run_rec.jobs_normalized = result.jobs_normalized
                    run_rec.new_opportunities = result.new_opportunities
                    run_rec.new_high_value_opportunities = high_value_count
                    run_rec.duplicates_ignored = result.duplicates_ignored
                    run_rec.rejected_low_score = result.rejected_low_score
                    run_rec.provider_metrics_json = prov_metrics
                    if result.errors_by_provider:
                        run_rec.errors_json = {"errors": result.errors_by_provider, "trigger_source": trigger_source}

                except asyncio.TimeoutError:
                    ended_at = datetime.now(timezone.utc).replace(tzinfo=None)
                    run_rec.status = DiscoveryRunStatus.failed
                    run_rec.finished_at = ended_at
                    run_rec.errors_json = {
                        "error": f"Discovery run timed out after {settings.discovery_scheduler_max_runtime_minutes} minutes.",
                        "trigger_source": trigger_source,
                    }
                    logger.error(run_rec.errors_json["error"])

                except Exception as e:
                    ended_at = datetime.now(timezone.utc).replace(tzinfo=None)
                    run_rec.status = DiscoveryRunStatus.failed
                    run_rec.finished_at = ended_at
                    run_rec.errors_json = {"error": str(e), "trigger_source": trigger_source}
                    logger.error(f"Discovery run failed with exception: {e}", exc_info=True)

                db.commit()
                db.refresh(run_rec)
                return run_rec
        finally:
            db.close()


scheduler_manager = DiscoverySchedulerManager()

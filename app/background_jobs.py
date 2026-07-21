"""Resilient, single-owner execution for periodic backend jobs."""

from __future__ import annotations

import logging
import os
import socket
import uuid
from collections.abc import Callable, Iterable
from datetime import timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.models import BackgroundJobLease, utcnow

logger = logging.getLogger(__name__)

SCHEDULER_JOB_NAME = "platform-periodic-jobs"


def scheduler_owner_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:12]}"


def acquire_scheduler_lease(
    db: Session,
    *,
    owner_id: str,
    now=None,
    interval_seconds: int | None = None,
    crash_lease_seconds: int = 300,
) -> bool:
    """Atomically claim the next scheduler slot.

    ``next_run_at`` controls cadence while ``lease_until`` protects a running
    cycle. A crashed worker becomes eligible again after the crash lease.
    """
    now = now or utcnow()
    interval = max(interval_seconds or settings.background_jobs_interval_seconds, 10)
    lease_until = now + timedelta(seconds=max(crash_lease_seconds, interval))
    next_run_at = now + timedelta(seconds=interval)
    row = db.query(BackgroundJobLease).filter(
        BackgroundJobLease.job_name == SCHEDULER_JOB_NAME
    ).first()
    if not row:
        try:
            db.add(
                BackgroundJobLease(
                    job_name=SCHEDULER_JOB_NAME,
                    owner_id=owner_id,
                    lease_until=lease_until,
                    next_run_at=next_run_at,
                )
            )
            db.commit()
            return True
        except IntegrityError:
            # Another process inserted the singleton lease concurrently.
            db.rollback()

    claimed = (
        db.query(BackgroundJobLease)
        .filter(
            BackgroundJobLease.job_name == SCHEDULER_JOB_NAME,
            BackgroundJobLease.lease_until <= now,
            BackgroundJobLease.next_run_at <= now,
        )
        .update(
            {
                "owner_id": owner_id,
                "lease_until": lease_until,
                "next_run_at": next_run_at,
                "updated_at": now,
            },
            synchronize_session=False,
        )
    )
    db.commit()
    return claimed == 1


def release_scheduler_lease(db: Session, *, owner_id: str, now=None) -> None:
    """Release only this worker's lease while retaining the next-run cadence."""
    now = now or utcnow()
    db.query(BackgroundJobLease).filter(
        BackgroundJobLease.job_name == SCHEDULER_JOB_NAME,
        BackgroundJobLease.owner_id == owner_id,
    ).update(
        {"lease_until": now, "updated_at": now},
        synchronize_session=False,
    )
    db.commit()


def default_periodic_jobs() -> tuple[tuple[str, Callable[[Session], object]], ...]:
    # Lazy imports avoid circular imports during app startup and migrations.
    from app.notification_jobs import process_scheduled_notifications
    from app.order_jobs import process_auto_confirm_orders
    from app.routers.platform_features import process_queued_media

    return (
        ("auto_confirm_orders", process_auto_confirm_orders),
        ("scheduled_notifications", process_scheduled_notifications),
        ("queued_media", process_queued_media),
    )


def run_periodic_cycle(
    db: Session,
    *,
    owner_id: str,
    jobs: Iterable[tuple[str, Callable[[Session], object]]] | None = None,
    now=None,
    interval_seconds: int | None = None,
) -> dict[str, object]:
    """Run one exclusive cycle, isolating failures so later jobs still execute."""
    if not acquire_scheduler_lease(
        db,
        owner_id=owner_id,
        now=now,
        interval_seconds=interval_seconds,
    ):
        return {"acquired": False, "completed": [], "failed": []}

    completed: list[str] = []
    failed: list[str] = []
    try:
        for name, job in jobs or default_periodic_jobs():
            try:
                job(db)
                completed.append(name)
            except Exception:  # noqa: BLE001 - scheduler must survive job failures
                db.rollback()
                failed.append(name)
                logger.exception("Periodic job %s failed; continuing the scheduler cycle", name)
    finally:
        try:
            release_scheduler_lease(db, owner_id=owner_id)
        except Exception:  # noqa: BLE001 - a crashed lease recovers by expiration
            db.rollback()
            logger.exception("Could not release periodic scheduler lease")
    return {"acquired": True, "completed": completed, "failed": failed}

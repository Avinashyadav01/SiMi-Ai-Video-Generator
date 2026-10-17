"""Per-user schedule ticker that dispatches durable topic-generation jobs."""
from __future__ import annotations

import asyncio
import logging
import os

import database
import publishing_service

logger = logging.getLogger("scheduler")


async def run_scheduler(stop: asyncio.Event, interval: float = 20.0) -> None:
    while not stop.is_set():
        try:
            count = await asyncio.to_thread(database.dispatch_due_ideas)
            if count:
                logger.info("scheduled topic ideas queued count=%d", count)
            if os.getenv("PUBLISHING_ENABLED", "false").strip().lower() in {"1", "true", "yes"}:
                due = await asyncio.to_thread(database.list_due_publish_jobs)
                semaphore = asyncio.Semaphore(4)

                async def publish_one(job: dict, platform: str) -> None:
                    try:
                        connection = await asyncio.to_thread(database.get_platform_connection, job["user_id"], platform)
                        if not connection or connection.get("status") != "CONNECTED" or not connection.get("enabled"):
                            return
                        async with semaphore:
                            await asyncio.to_thread(publishing_service.publish_job_platform, job["user_id"], job["id"], platform)
                            logger.info("scheduled publish confirmed job_id=%s platform=%s", job["id"], platform)
                    except Exception as exc:
                        logger.error("scheduled publish failed job_id=%s platform=%s error=%s", job["id"], platform, exc)

                tasks = [publish_one(job, platform) for job in due for platform in job["due_platforms"]]
                if tasks:
                    await asyncio.gather(*tasks)
        except Exception:
            logger.exception("schedule tick failed; will retry at the next tick")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            pass

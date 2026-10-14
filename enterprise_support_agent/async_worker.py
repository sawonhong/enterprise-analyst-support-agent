"""Asynchronous background worker for non-blocking memory consolidation and audit indexing."""

import asyncio
from concurrent.futures import Future, ThreadPoolExecutor
import logging
from typing import Any, Callable, Dict, List

from enterprise_support_agent.storage import storage

logger = logging.getLogger("enterprise_support_agent.async_worker")


class AsyncMemoryWorker:
    """Executes non-blocking background persistence and memory consolidation tasks."""

    def __init__(self, max_workers: int = 4) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="EnterpriseSupportAsyncWorker",
        )
        self.completed_jobs: List[Dict[str, Any]] = []

    def submit_audit_consolidation(
        self,
        ticket_id: str,
        action_type: str,
        summary_notes: str,
    ) -> Future:
        """Schedule non-blocking SQLite audit consolidation on a background thread.

        Args:
            ticket_id: Mutated ticket ID.
            action_type: Executed action type.
            summary_notes: Summary of the state transition and SLA context.

        Returns:
            Concurrent Future representing the background execution.
        """

        def _background_job() -> Dict[str, Any]:
            try:
                storage.record_audit_event_async_safe(
                    ticket_id=ticket_id,
                    action_type=f"async_consolidate:{action_type}",
                    notes=summary_notes,
                )
                job_record = {
                    "ticket_id": ticket_id,
                    "action_type": action_type,
                    "status": "consolidated",
                }
                self.completed_jobs.append(job_record)
                return job_record
            except Exception as exc:
                logger.error("[AsyncMemoryWorker] Consolidation error: %s", exc)
                return {"ticket_id": ticket_id, "status": "error", "error": str(exc)}

        return self._executor.submit(_background_job)

    async def async_consolidate_audit(
        self,
        ticket_id: str,
        action_type: str,
        summary_notes: str,
    ) -> Dict[str, Any]:
        """Await background audit consolidation using the active asyncio event loop."""
        loop = asyncio.get_running_loop()
        future = self.submit_audit_consolidation(ticket_id, action_type, summary_notes)
        return await loop.run_in_executor(None, future.result)

    def dispatch_fire_and_forget(self, func: Callable[..., Any], *args: Any) -> Future:
        """Submit an arbitrary callable to run asynchronously without blocking the turn."""
        return self._executor.submit(func, *args)


async_worker = AsyncMemoryWorker()

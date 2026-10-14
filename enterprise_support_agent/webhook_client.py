"""Simulated REST Webhook & Ticketing API client with retry resilience."""

from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import time
from typing import Any, Dict, List
import uuid

from enterprise_support_agent.config import PROJECT_ROOT, settings
from enterprise_support_agent.telemetry import PIIRedactor

logger = logging.getLogger("enterprise_support_agent.webhook")


class TicketingWebhookClient:
    """Dispatches structured REST webhook events to external ticketing/paging systems."""

    def __init__(self, endpoint_url: str = "") -> None:
        self.endpoint_url = endpoint_url or settings.webhook_endpoint_url
        self.outbox_path = PROJECT_ROOT / "data" / "webhook_outbox.json"
        self.dispatched_events: List[Dict[str, Any]] = []

    def dispatch_ticket_event(
        self,
        ticket_id: str,
        event_type: str,
        priority: str,
        assigned_team: str,
        payload: Dict[str, Any],
        max_retries: int = 3,
    ) -> Dict[str, Any]:
        """Send a signed webhook event with exponential backoff retry protection.

        Args:
            ticket_id: Target ticket ID (e.g., 'TKT-1001').
            event_type: Webhook event name (e.g., 'ticket.escalated', 'ticket.replied').
            priority: Assigned priority ('P0'-'P3').
            assigned_team: Target on-call or support team.
            payload: Event body to sanitize and transmit.
            max_retries: Maximum delivery attempts before fallback.

        Returns:
            Dictionary describing webhook delivery status, event_id, and signature.
        """
        event_id = f"wh_evt_{uuid.uuid4().hex[:12]}"
        sanitized_payload = PIIRedactor.redact_dict(payload)
        timestamp = datetime.now(timezone.utc).isoformat()
        body_bytes = json.dumps(sanitized_payload, sort_keys=True).encode("utf-8")
        signature = hashlib.sha256(body_bytes).hexdigest()[:24]

        attempt = 0
        while attempt < max_retries:
            attempt += 1
            try:
                envelope = {
                    "event_id": event_id,
                    "endpoint": self.endpoint_url,
                    "event_type": event_type,
                    "ticket_id": ticket_id,
                    "priority": priority,
                    "assigned_team": assigned_team,
                    "http_status": 200,
                    "delivery_status": "delivered",
                    "attempts": attempt,
                    "hmac_sha256_prefix": signature,
                    "timestamp": timestamp,
                    "payload": sanitized_payload,
                }
                self.dispatched_events.append(envelope)
                self._persist_outbox()
                logger.info(
                    "[WebhookClient] Dispatched %s for %s -> %s (event_id=%s)",
                    event_type,
                    ticket_id,
                    assigned_team,
                    event_id,
                )
                return envelope
            except Exception as exc:
                backoff_sec = 0.05 * (2 ** (attempt - 1))
                logger.warning(
                    "[WebhookClient] Attempt %d failed (%s); retrying in %.2fs",
                    attempt,
                    exc,
                    backoff_sec,
                )
                time.sleep(backoff_sec)

        return {
            "event_id": event_id,
            "endpoint": self.endpoint_url,
            "event_type": event_type,
            "ticket_id": ticket_id,
            "http_status": 503,
            "delivery_status": "queued_for_retry",
            "attempts": max_retries,
            "timestamp": timestamp,
        }

    def _persist_outbox(self) -> None:
        """Persist dispatched webhook payloads to local JSON outbox for inspection."""
        try:
            self.outbox_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.outbox_path, "w", encoding="utf-8") as file_obj:
                json.dump(self.dispatched_events, file_obj, indent=2)
        except Exception as exc:
            logger.debug("Webhook outbox write skipped: %s", exc)


webhook_client = TicketingWebhookClient()

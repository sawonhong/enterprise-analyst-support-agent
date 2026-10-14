"""Stateful SQLite and JSON persistence for support tickets, audit trails, and webhooks."""

from contextlib import closing
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import sqlite3
from typing import Any, Dict, List, Optional

from enterprise_support_agent.config import settings
from enterprise_support_agent.telemetry import PIIRedactor

logger = logging.getLogger("enterprise_support_agent.storage")


class SupportStateStorage:
    """Manages persistent SQLite tables and JSON state snapshots for ticket mutations."""

    def __init__(self, db_path: Optional[str] = None, json_path: Optional[str] = None) -> None:
        self.db_path = db_path or settings.sqlite_db_path
        self.json_path = json_path or settings.mutations_json_path
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_database()

    def _get_connection(self) -> sqlite3.Connection:
        """Create a SQLite connection with row dictionary access."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_database(self) -> None:
        """Initialize SQLite schema for ticket state tracking and audit history."""
        with closing(self._get_connection()) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ticket_states (
                    ticket_id TEXT PRIMARY KEY,
                    customer_id TEXT,
                    customer_tier TEXT,
                    issue_category TEXT,
                    status TEXT NOT NULL,
                    priority TEXT NOT NULL,
                    assigned_team TEXT NOT NULL,
                    escalation_flag INTEGER NOT NULL DEFAULT 0,
                    sla_status TEXT,
                    sentiment TEXT,
                    drafted_reply TEXT,
                    webhook_event_id TEXT,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ticket_audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticket_id TEXT NOT NULL,
                    action_type TEXT NOT NULL,
                    previous_status TEXT,
                    new_status TEXT,
                    priority TEXT,
                    assigned_team TEXT,
                    notes TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def upsert_ticket_mutation(
        self,
        ticket_id: str,
        customer_id: str,
        customer_tier: str,
        issue_category: str,
        previous_status: str,
        new_status: str,
        priority: str,
        assigned_team: str,
        escalation_flag: bool,
        sla_status: str,
        sentiment: str,
        drafted_reply: str,
        webhook_event_id: str,
        action_type: str,
        notes: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Persist a ticket mutation and append an immutable audit log entry.

        Args:
            ticket_id: Unique ticket ID (e.g., 'TKT-1001').
            customer_id: Customer account ID.
            customer_tier: Tier ('Enterprise', 'Business', 'Pro', 'Free').
            issue_category: Issue category label.
            previous_status: Status prior to this action.
            new_status: New ticket status ('escalated', 'in_progress', 'resolved').
            priority: Assigned priority ('P0'-'P3').
            assigned_team: Assigned specialist team.
            escalation_flag: Whether urgent escalation was triggered.
            sla_status: Computed SLA state ('breached', 'at_risk', 'within_sla').
            sentiment: Detected customer sentiment.
            drafted_reply: Sanitized customer response text.
            webhook_event_id: Dispatched REST webhook ID.
            action_type: Action performed ('escalate', 'reply', 'triage_and_reply', 'resolve').
            notes: Optional operator or custom notes.

        Returns:
            Dictionary representing the persisted ticket record.
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        sanitized_reply = PIIRedactor.redact(drafted_reply)
        sanitized_notes = PIIRedactor.redact(notes or "")

        with closing(self._get_connection()) as conn:
            conn.execute(
                """
                INSERT INTO ticket_states (
                    ticket_id, customer_id, customer_tier, issue_category,
                    status, priority, assigned_team, escalation_flag,
                    sla_status, sentiment, drafted_reply, webhook_event_id, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(ticket_id) DO UPDATE SET
                    status = excluded.status,
                    priority = excluded.priority,
                    assigned_team = excluded.assigned_team,
                    escalation_flag = excluded.escalation_flag,
                    sla_status = excluded.sla_status,
                    sentiment = excluded.sentiment,
                    drafted_reply = excluded.drafted_reply,
                    webhook_event_id = excluded.webhook_event_id,
                    updated_at = excluded.updated_at
                """,
                (
                    ticket_id,
                    customer_id,
                    customer_tier,
                    issue_category,
                    new_status,
                    priority,
                    assigned_team,
                    1 if escalation_flag else 0,
                    sla_status,
                    sentiment,
                    sanitized_reply,
                    webhook_event_id,
                    now_iso,
                ),
            )
            conn.execute(
                """
                INSERT INTO ticket_audit_log (
                    ticket_id, action_type, previous_status, new_status,
                    priority, assigned_team, notes, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ticket_id,
                    action_type,
                    previous_status,
                    new_status,
                    priority,
                    assigned_team,
                    sanitized_notes,
                    now_iso,
                ),
            )
            conn.commit()

        record = self.get_ticket_state(ticket_id) or {}
        self._export_json_snapshot()
        return record

    def record_audit_event_async_safe(
        self,
        ticket_id: str,
        action_type: str,
        notes: str,
    ) -> None:
        """Write a background audit entry from the non-blocking AsyncMemoryWorker."""
        now_iso = datetime.now(timezone.utc).isoformat()
        with closing(self._get_connection()) as conn:
            conn.execute(
                """
                INSERT INTO ticket_audit_log (
                    ticket_id, action_type, previous_status, new_status,
                    priority, assigned_team, notes, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ticket_id,
                    action_type,
                    "async_sync",
                    "async_synced",
                    "INFO",
                    "AsyncMemoryWorker",
                    PIIRedactor.redact(notes),
                    now_iso,
                ),
            )
            conn.commit()

    def get_ticket_state(self, ticket_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve the persisted state and audit history for a specific ticket ID."""
        with closing(self._get_connection()) as conn:
            row = conn.execute(
                "SELECT * FROM ticket_states WHERE ticket_id = ?", (ticket_id,)
            ).fetchone()
            if not row:
                return None
            state_dict = dict(row)
            state_dict["escalation_flag"] = bool(state_dict.get("escalation_flag"))
            audits = conn.execute(
                "SELECT * FROM ticket_audit_log WHERE ticket_id = ? ORDER BY id ASC",
                (ticket_id,),
            ).fetchall()
            state_dict["audit_trail"] = [dict(a) for a in audits]
            return state_dict

    def get_all_ticket_states(self) -> List[Dict[str, Any]]:
        """Return all mutated tickets stored in SQLite."""
        with closing(self._get_connection()) as conn:
            rows = conn.execute(
                "SELECT * FROM ticket_states ORDER BY updated_at DESC"
            ).fetchall()
            results = []
            for row in rows:
                item = dict(row)
                item["escalation_flag"] = bool(item.get("escalation_flag"))
                results.append(item)
            return results

    def _export_json_snapshot(self) -> None:
        """Write a human-readable JSON snapshot of all mutated tickets."""
        try:
            all_states = self.get_all_ticket_states()
            Path(self.json_path).parent.mkdir(parents=True, exist_ok=True)
            with open(self.json_path, "w", encoding="utf-8") as file_obj:
                json.dump(
                    {
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                        "total_mutated_tickets": len(all_states),
                        "tickets": all_states,
                    },
                    file_obj,
                    indent=2,
                )
        except Exception as exc:
            logger.debug("Snapshot export skipped: %s", exc)


storage = SupportStateStorage()

"""Stateful multi-turn conversation memory and contextual entity resolution."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import re
from typing import Any, Dict, List, Optional


@dataclass
class ConversationSession:
    """Stores multi-turn conversation history and active troubleshooting context."""

    session_id: str
    user_id: str = "enterprise_operator"
    turns: List[Dict[str, str]] = field(default_factory=list)
    active_ticket_id: Optional[str] = None
    recent_ticket_ids: List[str] = field(default_factory=list)
    last_tier_filter: Optional[str] = None
    last_analysis_summary: Optional[Dict[str, Any]] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class SessionMemoryManager:
    """Manages short-term sliding-window memory and entity co-reference across turns."""

    TICKET_ID_REGEX = re.compile(r"\b(TKT-\d{4,})\b", re.IGNORECASE)

    def __init__(self, max_window_turns: int = 8) -> None:
        self.max_window_turns = max_window_turns
        self._sessions: Dict[str, ConversationSession] = {}

    def get_or_create_session(
        self, session_id: str = "default_session", user_id: str = "enterprise_operator"
    ) -> ConversationSession:
        """Retrieve an existing session or initialize a new stateful session."""
        if session_id not in self._sessions:
            self._sessions[session_id] = ConversationSession(
                session_id=session_id, user_id=user_id
            )
        return self._sessions[session_id]

    def append_turn(
        self,
        session_id: str,
        user_message: str,
        agent_response: str,
        active_ticket_id: Optional[str] = None,
        analysis_summary: Optional[Dict[str, Any]] = None,
    ) -> ConversationSession:
        """Record a completed turn and update contextual entity pointers.

        Args:
            session_id: Identifier of the conversation session.
            user_message: User query for this turn.
            agent_response: Agent response for this turn.
            active_ticket_id: Optional primary ticket ID discussed or mutated.
            analysis_summary: Optional structured output from analyze_customer_data.

        Returns:
            Updated ConversationSession instance.
        """
        session = self.get_or_create_session(session_id)
        session.turns.append({"role": "user", "content": user_message})
        session.turns.append({"role": "assistant", "content": agent_response})

        # Keep sliding window bounded to max_window_turns * 2 messages
        max_messages = self.max_window_turns * 2
        if len(session.turns) > max_messages:
            session.turns = session.turns[-max_messages:]

        if active_ticket_id:
            normalized_id = active_ticket_id.upper()
            session.active_ticket_id = normalized_id
            if normalized_id not in session.recent_ticket_ids:
                session.recent_ticket_ids.insert(0, normalized_id)

        if analysis_summary:
            session.last_analysis_summary = analysis_summary
            critical_list = analysis_summary.get("critical_tickets", [])
            if critical_list and not active_ticket_id:
                top_critical_id = critical_list[0].get("ticket_id")
                if top_critical_id:
                    session.active_ticket_id = top_critical_id
                    if top_critical_id not in session.recent_ticket_ids:
                        session.recent_ticket_ids.insert(0, top_critical_id)

        return session

    def resolve_contextual_ticket_id(
        self, session_id: str, user_message: str
    ) -> Optional[str]:
        """Resolve explicit or pronoun-referenced ticket IDs from session memory.

        Args:
            session_id: Active conversation session ID.
            user_message: Current user message (may say 'that ticket' or 'the critical one').

        Returns:
            Resolved ticket ID (e.g., 'TKT-1001') if found in message or memory.
        """
        explicit_match = self.TICKET_ID_REGEX.search(user_message)
        if explicit_match:
            return explicit_match.group(1).upper()

        session = self.get_or_create_session(session_id)
        msg_lower = user_message.lower()

        # Check if user refers to a specific category or attribute from the last analysis
        if session.last_analysis_summary:
            tickets = session.last_analysis_summary.get("tickets", [])
            for ticket in tickets:
                category = str(ticket.get("issue_category", "")).lower()
                if category and category in msg_lower:
                    return ticket.get("ticket_id")
                if "outage" in msg_lower and "outage" in category:
                    return ticket.get("ticket_id")
                if "sso" in msg_lower and "sso" in category:
                    return ticket.get("ticket_id")
                if "billing" in msg_lower and "billing" in category:
                    return ticket.get("ticket_id")
                if "rate limit" in msg_lower and "rate limit" in category:
                    return ticket.get("ticket_id")

        if session.active_ticket_id:
            return session.active_ticket_id

        if session.recent_ticket_ids:
            return session.recent_ticket_ids[0]

        return None

    def get_context_window(self, session_id: str) -> List[Dict[str, str]]:
        """Return the recent conversation turns for LLM context injection."""
        session = self.get_or_create_session(session_id)
        return list(session.turns)

    def clear_session(self, session_id: str = "default_session") -> None:
        """Reset a conversation session's short-term memory."""
        if session_id in self._sessions:
            del self._sessions[session_id]


memory_manager = SessionMemoryManager()

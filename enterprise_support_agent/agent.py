"""Multi-Agent Python ADK Orchestrator with Supervisor-Worker Topology, Model Routing, and Guardrails."""

from dataclasses import dataclass, field
import logging
import re
import time
from typing import Any, Callable, Dict, List, Optional
import uuid

from enterprise_support_agent.config import settings
from enterprise_support_agent.guardrails import (
    hitl_manager,
    input_guardrail,
    output_guardrail,
)
from enterprise_support_agent.memory import memory_manager
from enterprise_support_agent.prompts import ENTERPRISE_SUPPORT_SYSTEM_PROMPT
from enterprise_support_agent.telemetry import (
    ToolExecutionTrace,
    TurnTrace,
    tracer,
)
from enterprise_support_agent.tools import (
    ALL_TOOLS,
    analyze_customer_data,
    escalate_or_reply_ticket,
    generate_sla_executive_digest,
    get_ticket_history_and_status,
)

logger = logging.getLogger("enterprise_support_agent.orchestrator")

# -----------------------------------------------------------------------------
# 1. Google ADK (`google.adk`) Integration & Compatibility Layer
# -----------------------------------------------------------------------------
try:
    from google.adk import Agent  # type: ignore
    from google.adk.runners import InMemoryRunner  # type: ignore
    from google.adk.sessions import InMemorySessionService  # type: ignore
    from google.adk.tools import FunctionTool  # type: ignore

    HAS_GOOGLE_ADK = True
except ImportError:
    HAS_GOOGLE_ADK = False

    class FunctionTool:  # type: ignore
        """Compatibility wrapper for google.adk.tools.FunctionTool."""

        def __init__(self, func: Callable[..., Any]) -> None:
            self.func = func
            self.name = getattr(func, "__name__", "tool")
            self.description = getattr(func, "__doc__", "") or ""

        def __call__(self, *args: Any, **kwargs: Any) -> Any:
            return self.func(*args, **kwargs)

    class Agent:  # type: ignore
        """Compatibility wrapper for google.adk.Agent (LlmAgent)."""

        def __init__(
            self,
            model: str,
            name: str,
            description: str = "",
            instruction: str = "",
            tools: Optional[List[Any]] = None,
            sub_agents: Optional[List[Any]] = None,
            output_key: Optional[str] = None,
        ) -> None:
            self.model = model
            self.name = name
            self.description = description
            self.instruction = instruction
            self.tools = tools or []
            self.sub_agents = sub_agents or []
            self.output_key = output_key

    class InMemorySessionService:  # type: ignore
        """Compatibility wrapper for google.adk.sessions.InMemorySessionService."""

        def __init__(self) -> None:
            self.sessions: Dict[str, Any] = {}

    class InMemoryRunner:  # type: ignore
        """Compatibility wrapper for google.adk.runners.InMemoryRunner."""

        def __init__(self, app_name: str, agent: Agent) -> None:
            self.app_name = app_name
            self.agent = agent
            self.session_service = InMemorySessionService()


# Wrap tools as explicit ADK FunctionTools
analyze_customer_data_tool = FunctionTool(analyze_customer_data)
escalate_or_reply_ticket_tool = FunctionTool(escalate_or_reply_ticket)
get_ticket_history_tool = FunctionTool(get_ticket_history_and_status)
generate_executive_digest_tool = FunctionTool(generate_sla_executive_digest)

# Define Specialized Sub-Agents and Root ADK Agent
data_analyst_agent = Agent(
    model=settings.synthesis_model,
    name="customer_data_analyst_agent",
    description="Analyzes customer support datasets for SLA breaches, sentiment, and data quality.",
    instruction=ENTERPRISE_SUPPORT_SYSTEM_PROMPT,
    tools=[analyze_customer_data_tool],
    output_key="last_analysis_result",
)

ticket_escalation_agent = Agent(
    model=settings.synthesis_model,
    name="ticket_escalation_agent",
    description="Executes stateful ticket escalations, customer reply drafting, and webhook notifications.",
    instruction=ENTERPRISE_SUPPORT_SYSTEM_PROMPT,
    tools=[escalate_or_reply_ticket_tool, get_ticket_history_tool],
    output_key="last_mutation_result",
)

executive_digest_agent = Agent(
    model=settings.synthesis_model,
    name="executive_digest_agent",
    description="Synthesizes executive SLA compliance digests and remediation recommendations.",
    instruction=ENTERPRISE_SUPPORT_SYSTEM_PROMPT,
    tools=[generate_executive_digest_tool],
    output_key="last_digest_result",
)

root_agent = Agent(
    model=settings.synthesis_model,
    name="enterprise_support_coordinator",
    description=(
        "Principal Enterprise Support & Data Analysis Coordinator routing queries across "
        "data analysis, stateful ticket escalation, and SLA executive reporting."
    ),
    instruction=ENTERPRISE_SUPPORT_SYSTEM_PROMPT,
    tools=[
        analyze_customer_data_tool,
        escalate_or_reply_ticket_tool,
        get_ticket_history_tool,
        generate_executive_digest_tool,
    ],
    sub_agents=[
        data_analyst_agent,
        ticket_escalation_agent,
        executive_digest_agent,
    ],
)


# -----------------------------------------------------------------------------
# 2. Typed Inter-Agent State Protocol & Autonomous Worker Agents
# -----------------------------------------------------------------------------
@dataclass
class AgentState:
    """Shared typed state passed between the Supervisor Router and autonomous workers."""

    session_id: str
    user_query: str
    routed_intent: str = "DATA_ANALYSIS"
    selected_worker: str = "CustomerDataAnalystWorker"
    resolved_ticket_id: Optional[str] = None
    active_ticket_id: Optional[str] = None
    analysis_summary: Optional[Dict[str, Any]] = None
    intermediate_steps: List[Dict[str, Any]] = field(default_factory=list)
    agent_handoffs: List[str] = field(default_factory=list)
    response_text: str = ""
    is_completed: bool = False


class BaseAutonomousWorker:
    """Abstract base class for specialized autonomous worker agents."""

    def __init__(self, name: str, role_description: str, model: str) -> None:
        self.name = name
        self.role_description = role_description
        self.model = model

    def process(self, state: AgentState) -> AgentState:
        """Process the shared AgentState and populate response_text."""
        raise NotImplementedError


class CustomerDataAnalystWorker(BaseAutonomousWorker):
    """Autonomous worker specialized in dataset metrics, SLA triage, and anomaly detection."""

    def __init__(self) -> None:
        super().__init__(
            name="CustomerDataAnalystWorker",
            role_description="Executes analyze_customer_data and highlights critical SLA and sentiment insights.",
            model=settings.synthesis_model,
        )

    def process(self, state: AgentState) -> AgentState:
        state.agent_handoffs.append(self.name)
        query_lower = state.user_query.lower()

        tier_filter: Optional[str] = None
        for tier in ("Enterprise", "Business", "Pro", "Free"):
            if re.search(rf"\b{tier.lower()}\b", query_lower):
                tier_filter = tier
                break

        explicit_ticket_match = re.search(r"\b(TKT-\d{4,})\b", state.user_query, re.I)
        ticket_filter = (
            explicit_ticket_match.group(1).upper()
            if (explicit_ticket_match and "dataset" not in query_lower)
            else None
        )

        analysis = analyze_customer_data(
            customer_tier=tier_filter,
            ticket_id=ticket_filter,
        )
        state.intermediate_steps.append(
            {"worker": self.name, "tool": "analyze_customer_data", "result": analysis}
        )

        if analysis.get("status") != "success":
            state.response_text = (
                f"⚠️ **Data Analysis Notice**: {analysis.get('message')} "
                f"Recovery Guidance: {analysis.get('recovery_guidance')}"
            )
            state.is_completed = True
            return state

        state.analysis_summary = analysis
        critical = analysis.get("critical_tickets", [])
        if critical:
            # Prefer a P0 Production Outage or first critical ticket as active context
            outage_tickets = [
                t for t in critical if "outage" in t["issue_category"].lower()
            ]
            primary = outage_tickets[0] if outage_tickets else critical[0]
            state.active_ticket_id = primary["ticket_id"]
        elif analysis.get("tickets"):
            state.active_ticket_id = analysis["tickets"][0]["ticket_id"]

        scope_label = f"{tier_filter} Tier" if tier_filter else "All Customer Tiers"
        crit_bullets = "\n".join(
            f"- **{t['ticket_id']}** (`{t['customer_tier']}` | Priority `{t['priority']}` | "
            f"SLA `{t['sla_status']}` at {t['elapsed_hours']}h/{t['sla_hours']}h | "
            f"Sentiment `{t['sentiment']}`): {t['issue_category']} — *{t['message']}*"
            for t in critical
        )
        anomaly_bullets = "\n".join(
            f"- **{a['ticket_id']}** (`{a['field']}`): {a['issue']}"
            for a in analysis.get("data_quality_issues", [])
        )

        state.response_text = (
            f"### 📈 Customer Support Data Analysis ({scope_label})\n\n"
            f"- **Total Tickets Analyzed**: {analysis['total_tickets_analyzed']}\n"
            f"- **SLA Compliance Rate**: {analysis['sla_compliance_rate_pct']}% "
            f"(Breached: {analysis['sla_breached_count']}, At-Risk: {analysis['sla_at_risk_count']})\n"
            f"- **Priority Breakdown**: {analysis['priority_breakdown']}\n"
            f"- **Sentiment Breakdown**: {analysis['sentiment_breakdown']}\n\n"
            f"#### 🚨 Critical & High-Priority Tickets\n"
            f"{crit_bullets or '- None'}\n"
        )
        if anomaly_bullets:
            state.response_text += (
                f"\n#### 🔍 Data Quality & Edge-Case Anomalies\n{anomaly_bullets}\n"
            )
        state.is_completed = True
        return state


class TicketEscalationAndReplyWorker(BaseAutonomousWorker):
    """Autonomous worker specialized in stateful ticket escalation, reply drafting, and webhooks."""

    def __init__(self) -> None:
        super().__init__(
            name="TicketEscalationAndReplyWorker",
            role_description="Executes escalate_or_reply_ticket and persists mutations to SQLite + REST webhook.",
            model=settings.synthesis_model,
        )

    def process(self, state: AgentState) -> AgentState:
        state.agent_handoffs.append(self.name)
        query_lower = state.user_query.lower()

        target_ticket_id = state.resolved_ticket_id
        if not target_ticket_id:
            # Run quick analysis to locate the most critical ticket
            analysis = analyze_customer_data()
            if analysis.get("critical_tickets"):
                target_ticket_id = analysis["critical_tickets"][0]["ticket_id"]
            else:
                target_ticket_id = "TKT-1001"

        if "resolve" in query_lower or "close" in query_lower:
            action_type = "resolve"
        elif "escalate" in query_lower or "p0" in query_lower or "p1" in query_lower:
            action_type = "escalate"
        elif "reply" in query_lower and "triage" not in query_lower:
            action_type = "reply"
        else:
            action_type = "triage_and_reply"

        prio_override: Optional[str] = None
        for prio in ("P0", "P1", "P2", "P3"):
            if prio.lower() in query_lower:
                prio_override = prio
                break

        mutation = escalate_or_reply_ticket(
            ticket_id=target_ticket_id,
            action_type=action_type,
            priority=prio_override,
        )
        state.intermediate_steps.append(
            {
                "worker": self.name,
                "tool": "escalate_or_reply_ticket",
                "result": mutation,
            }
        )

        if mutation.get("status") != "success":
            state.response_text = (
                f"⚠️ **Ticket Action Error ({target_ticket_id})**: {mutation.get('message')}\n"
                f"💡 **Recovery Guidance**: {mutation.get('recovery_guidance')}"
            )
            state.is_completed = True
            return state

        state.active_ticket_id = mutation["ticket_id"]
        wh = mutation["webhook_delivery"]
        state.response_text = (
            f"### ✅ Ticket Action Executed: `{mutation['ticket_id']}`\n\n"
            f"- **Status Transition**: `{mutation['previous_status']}` ➡️ `{mutation['new_status']}`\n"
            f"- **Assigned Priority**: `{mutation['assigned_priority']}`\n"
            f"- **Routed Specialist Queue**: `{mutation['assigned_team']}`\n"
            f"- **Escalation Flag**: `{mutation['escalation_flag']}`\n"
            f"- **Webhook Delivery**: `{wh['delivery_status']}` "
            f"(Event ID: `{wh['event_id']}`, HTTP `{wh['http_status']}`)\n\n"
            f"#### ✉️ Drafted Customer Reply\n"
            f"> {mutation['drafted_customer_reply']}"
        )
        state.is_completed = True
        return state


class TicketStatusAuditWorker(BaseAutonomousWorker):
    """Autonomous worker specialized in inspecting persisted SQLite state and audit trails."""

    def __init__(self) -> None:
        super().__init__(
            name="TicketStatusAuditWorker",
            role_description="Retrieves live ticket state and audit history via get_ticket_history_and_status.",
            model=settings.synthesis_model,
        )

    def process(self, state: AgentState) -> AgentState:
        state.agent_handoffs.append(self.name)
        lookup = get_ticket_history_and_status(ticket_id=state.resolved_ticket_id)
        state.intermediate_steps.append(
            {
                "worker": self.name,
                "tool": "get_ticket_history_and_status",
                "result": lookup,
            }
        )

        if lookup.get("status") != "success":
            state.response_text = (
                f"⚠️ **Status Lookup Notice**: {lookup.get('message')}\n"
                f"💡 **Recovery Guidance**: {lookup.get('recovery_guidance')}"
            )
            state.is_completed = True
            return state

        if "ticket" in lookup:
            ticket = lookup["ticket"]
            state.active_ticket_id = ticket["ticket_id"]
            audits = ticket.get("audit_trail", [])
            audit_lines = "\n".join(
                f"  - `[{a['created_at']}]` Action `{a['action_type']}`: "
                f"`{a['previous_status']}` -> `{a['new_status']}` (Priority `{a['priority']}`, Team `{a['assigned_team']}`)"
                for a in audits
            )
            state.response_text = (
                f"### 🗂️ Stateful Ticket Record: `{ticket['ticket_id']}`\n\n"
                f"- **Current Status**: `{ticket['status']}`\n"
                f"- **Priority**: `{ticket['priority']}`\n"
                f"- **Assigned Team**: `{ticket['assigned_team']}`\n"
                f"- **Escalation Flag**: `{ticket.get('escalation_flag')}`\n"
                f"- **Webhook Event ID**: `{ticket.get('webhook_event_id', 'N/A')}`\n"
                f"- **Drafted Reply**: {ticket.get('drafted_reply') or 'None'}\n\n"
                f"#### 📜 Audit Trail ({len(audits)} entries)\n"
                f"{audit_lines or '  - No state mutations recorded yet.'}"
            )
        else:
            tickets = lookup.get("tickets", [])
            lines = "\n".join(
                f"- **{t['ticket_id']}**: status=`{t['status']}`, "
                f"priority=`{t['priority']}`, team=`{t['assigned_team']}`"
                for t in tickets
            )
            state.response_text = (
                f"### 🗂️ All Mutated Tickets in SQLite ({lookup.get('total_mutated_tickets', 0)})\n\n"
                f"{lines or 'No tickets mutated yet.'}"
            )
        state.is_completed = True
        return state


class SLAComplianceDigestWorker(BaseAutonomousWorker):
    """Autonomous worker specialized in generating executive SLA compliance digests."""

    def __init__(self) -> None:
        super().__init__(
            name="SLAComplianceDigestWorker",
            role_description="Executes generate_sla_executive_digest for executive reporting.",
            model=settings.synthesis_model,
        )

    def process(self, state: AgentState) -> AgentState:
        state.agent_handoffs.append(self.name)
        digest = generate_sla_executive_digest(include_recommendations=True)
        state.intermediate_steps.append(
            {
                "worker": self.name,
                "tool": "generate_sla_executive_digest",
                "result": digest,
            }
        )
        state.response_text = digest.get(
            "markdown_report", "Failed to generate executive digest."
        )
        state.is_completed = True
        return state


# -----------------------------------------------------------------------------
# 3. Central Supervisor Orchestrator
# -----------------------------------------------------------------------------
class EnterpriseSupportOrchestrator:
    """Central Supervisor orchestrating ADK tools, multi-agent workers, memory, and guardrails."""

    VALID_INTENTS = {
        "DATA_ANALYSIS",
        "TICKET_ESCALATION",
        "TICKET_STATUS_LOOKUP",
        "EXECUTIVE_DIGEST",
        "MULTI_STEP_TRIAGE",
    }

    def __init__(
        self,
        api_key: Optional[str] = None,
        router_model: Optional[str] = None,
        synthesis_model: Optional[str] = None,
    ) -> None:
        self.api_key = api_key or settings.active_api_key
        self.router_model = router_model or settings.router_model
        self.synthesis_model = synthesis_model or settings.synthesis_model
        self.system_prompt = ENTERPRISE_SUPPORT_SYSTEM_PROMPT
        self.adk_runner = InMemoryRunner(
            app_name=settings.app_name, agent=root_agent
        )

        self.workers: Dict[str, BaseAutonomousWorker] = {
            "DATA_ANALYSIS": CustomerDataAnalystWorker(),
            "TICKET_ESCALATION": TicketEscalationAndReplyWorker(),
            "TICKET_STATUS_LOOKUP": TicketStatusAuditWorker(),
            "EXECUTIVE_DIGEST": SLAComplianceDigestWorker(),
        }
        self.tool_map: Dict[str, Callable[..., Any]] = {
            "analyze_customer_data": analyze_customer_data,
            "escalate_or_reply_ticket": escalate_or_reply_ticket,
            "get_ticket_history_and_status": get_ticket_history_and_status,
            "generate_sla_executive_digest": generate_sla_executive_digest,
        }

    def route_intent(self, user_query: str) -> str:
        """Stage 1: Classify user query intent using lightweight router model or deterministic fallback."""
        if self.api_key:
            try:
                from google import genai  # type: ignore
                from google.genai import types  # type: ignore

                client = genai.Client(api_key=self.api_key)
                prompt = (
                    "Classify the enterprise support query into exactly ONE intent label:\n"
                    "- 'MULTI_STEP_TRIAGE': User wants BOTH data analysis AND immediate escalation/reply in one turn.\n"
                    "- 'TICKET_ESCALATION': User wants to escalate, reply to, triage, or resolve a ticket.\n"
                    "- 'TICKET_STATUS_LOOKUP': User asks for the current status, audit trail, or webhook history of a ticket.\n"
                    "- 'EXECUTIVE_DIGEST': User asks for an executive report or SLA compliance digest.\n"
                    "- 'DATA_ANALYSIS': User asks to analyze customer support data, metrics, sentiment, or SLA breaches.\n\n"
                    f"Query: {user_query}\nOutput ONLY the label."
                )
                resp = client.models.generate_content(
                    model=self.router_model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        temperature=0.0,
                        max_output_tokens=25,
                    ),
                )
                predicted = (resp.text or "").strip().strip("'\"")
                if predicted in self.VALID_INTENTS:
                    logger.info(
                        "[ModelRouter] %s classified intent as %s",
                        self.router_model,
                        predicted,
                    )
                    return predicted
            except Exception as exc:
                logger.debug(
                    "[ModelRouter] Live LLM router fallback triggered: %s", exc
                )

        return self._deterministic_route_intent(user_query)

    @staticmethod
    def _deterministic_route_intent(user_query: str) -> str:
        """Deterministic intent classification for offline and unit-test environments."""
        q = user_query.lower()
        has_analyze = any(
            w in q for w in ("analyze", "analysis", "metrics", "dataset", "which ticket")
        )
        has_escalate = any(
            w in q
            for w in ("escalate", "reply", "respond", "draft", "resolve", "p0", "p1")
        )
        if has_analyze and has_escalate:
            return "MULTI_STEP_TRIAGE"
        if any(
            w in q
            for w in ("audit trail", "current status", "history", "what is the status")
        ):
            return "TICKET_STATUS_LOOKUP"
        if any(w in q for w in ("executive digest", "executive report", "sla digest")):
            return "EXECUTIVE_DIGEST"
        if has_escalate:
            return "TICKET_ESCALATION"
        return "DATA_ANALYSIS"

    def chat(
        self,
        user_message: str,
        session_id: str = "default_session",
    ) -> Dict[str, Any]:
        """Execute a stateful multi-agent conversational turn with guardrails and tracing.

        Args:
            user_message: User query or command.
            session_id: Conversation session identifier for multi-turn continuity.

        Returns:
            Dictionary containing response text, turn_id, tool_calls, and agent_state.
        """
        turn_id = f"turn_{uuid.uuid4().hex[:12]}"
        start_perf = time.perf_counter()
        tool_traces_before = len(tracer.tool_traces)

        # 1. Pre-Execution Input Guardrail
        guard_check = input_guardrail.validate(user_message)
        if not guard_check.passed:
            blocked_msg = f"🛡️ **Security Guardrail Blocked Request**: {guard_check.reason}"
            duration_ms = round((time.perf_counter() - start_perf) * 1000, 2)
            turn_trace = TurnTrace(
                turn_id=turn_id,
                session_id=session_id,
                user_query=user_message,
                routed_intent="BLOCKED_BY_GUARDRAIL",
                selected_worker="InputGuardrail",
                thought_process=guard_check.reason,
                agent_response=blocked_msg,
                tool_calls=[],
                total_duration_ms=duration_ms,
            )
            tracer.record_turn(turn_trace)
            return {
                "turn_id": turn_id,
                "session_id": session_id,
                "status": "blocked",
                "response": blocked_msg,
                "tool_calls": [],
                "total_duration_ms": duration_ms,
            }

        sanitized_query = guard_check.sanitized_text or user_message

        # 2. Human-in-the-Loop (HITL) Gate for high-stakes operations
        if hitl_manager.requires_approval(
            "support_action_check", {"query": sanitized_query}
        ):
            approval_req = hitl_manager.create_approval_request(
                request_id=turn_id,
                action_name="high_stakes_support_operation",
                parameters={"query": sanitized_query, "session_id": session_id},
            )
            duration_ms = round((time.perf_counter() - start_perf) * 1000, 2)
            hitl_msg = f"⚠️ **Human-in-the-Loop Approval Required**: {approval_req['message']}"
            return {
                "turn_id": turn_id,
                "session_id": session_id,
                "status": "requires_approval",
                "approval_request": approval_req,
                "response": hitl_msg,
                "tool_calls": [],
                "total_duration_ms": duration_ms,
            }

        # 3. Resolve Contextual Ticket ID from Multi-Turn Session Memory
        resolved_ticket_id = memory_manager.resolve_contextual_ticket_id(
            session_id=session_id, user_message=sanitized_query
        )

        # 4. Stage 1: Intent Classification & Routing
        routed_intent = self.route_intent(sanitized_query)
        state = AgentState(
            session_id=session_id,
            user_query=sanitized_query,
            routed_intent=routed_intent,
            resolved_ticket_id=resolved_ticket_id,
        )

        # 5. Stage 2: Execute Live Gemini Tool-Calling or Deterministic Multi-Agent Graph
        if self.api_key:
            response_text = self._execute_with_gemini(
                sanitized_query=sanitized_query,
                session_id=session_id,
                state=state,
            )
        else:
            response_text = self._execute_worker_pipeline(state)

        # 6. Post-Execution Output Guardrail
        out_check = output_guardrail.validate(response_text)
        final_response = (
            out_check.sanitized_text
            if out_check.passed and out_check.sanitized_text
            else "I apologize, but I could not generate a grounded response."
        )

        # 7. Update Multi-Turn Session Memory
        memory_manager.append_turn(
            session_id=session_id,
            user_message=sanitized_query,
            agent_response=final_response,
            active_ticket_id=state.active_ticket_id or state.resolved_ticket_id,
            analysis_summary=state.analysis_summary,
        )

        turn_tool_traces = tracer.tool_traces[tool_traces_before:]
        total_duration_ms = round((time.perf_counter() - start_perf) * 1000, 2)
        thought_process = (
            f"Stage 1 Router ({self.router_model}) classified intent='{routed_intent}'. "
            f"Resolved context ticket='{state.active_ticket_id or state.resolved_ticket_id}'. "
            f"Handoffs={state.agent_handoffs}."
        )

        turn_trace = TurnTrace(
            turn_id=turn_id,
            session_id=session_id,
            user_query=user_message,
            routed_intent=routed_intent,
            selected_worker=" -> ".join(state.agent_handoffs) or state.selected_worker,
            thought_process=thought_process,
            agent_response=final_response,
            tool_calls=turn_tool_traces,
            total_duration_ms=total_duration_ms,
            token_estimate=len(sanitized_query.split()) + len(final_response.split()),
        )
        tracer.record_turn(turn_trace)

        return {
            "turn_id": turn_id,
            "session_id": session_id,
            "status": "success",
            "response": final_response,
            "tool_calls": [t.to_dict() for t in turn_tool_traces],
            "total_duration_ms": total_duration_ms,
            "agent_state": {
                "routed_intent": routed_intent,
                "active_ticket_id": memory_manager.get_or_create_session(
                    session_id
                ).active_ticket_id,
                "handoffs": state.agent_handoffs,
            },
        }

    def _execute_worker_pipeline(self, state: AgentState) -> str:
        """Execute autonomous worker agents over the shared AgentState."""
        if state.routed_intent == "MULTI_STEP_TRIAGE":
            analyst = self.workers["DATA_ANALYSIS"]
            state = analyst.process(state)
            analysis_part = state.response_text

            state.resolved_ticket_id = state.active_ticket_id or state.resolved_ticket_id
            escalator = self.workers["TICKET_ESCALATION"]
            state = escalator.process(state)
            return f"{analysis_part}\n\n---\n\n{state.response_text}"

        worker = self.workers.get(state.routed_intent, self.workers["DATA_ANALYSIS"])
        state.selected_worker = worker.name
        state = worker.process(state)
        return state.response_text

    def _execute_with_gemini(
        self,
        sanitized_query: str,
        session_id: str,
        state: AgentState,
    ) -> str:
        """Invoke Google GenAI SDK with bound ADK tools and automatic worker fallback."""
        try:
            from google import genai  # type: ignore
            from google.genai import types  # type: ignore

            client = genai.Client(api_key=self.api_key)
            contents: List[Any] = []
            for msg in memory_manager.get_context_window(session_id)[-6:]:
                role = "model" if msg["role"] == "assistant" else "user"
                contents.append(
                    types.Content(
                        role=role,
                        parts=[types.Part.from_text(text=msg["content"])],
                    )
                )

            context_hint = (
                f"[Context Active Ticket: {state.resolved_ticket_id}] "
                if state.resolved_ticket_id
                else ""
            )
            contents.append(
                types.Content(
                    role="user",
                    parts=[
                        types.Part.from_text(
                            text=f"[Intent: {state.routed_intent}] {context_hint}{sanitized_query}"
                        )
                    ],
                )
            )

            config = types.GenerateContentConfig(
                system_instruction=self.system_prompt,
                temperature=0.1,
                tools=ALL_TOOLS,
            )
            response = client.models.generate_content(
                model=self.synthesis_model,
                contents=contents,
                config=config,
            )

            if getattr(response, "function_calls", None):
                tool_outputs: List[str] = []
                for fn_call in response.function_calls:
                    fn_name = fn_call.name
                    fn_args = dict(fn_call.args) if fn_call.args else {}
                    if fn_name in self.tool_map:
                        state.agent_handoffs.append(f"GeminiToolCall:{fn_name}")
                        res = self.tool_map[fn_name](**fn_args)
                        if fn_name == "analyze_customer_data" and isinstance(res, dict):
                            state.analysis_summary = res
                            crit = res.get("critical_tickets", [])
                            if crit:
                                state.active_ticket_id = crit[0]["ticket_id"]
                        elif fn_name == "escalate_or_reply_ticket" and isinstance(
                            res, dict
                        ):
                            state.active_ticket_id = res.get("ticket_id")
                        tool_outputs.append(f"Tool `{fn_name}` output: {res}")

                contents.append(
                    types.Content(
                        role="user",
                        parts=[
                            types.Part.from_text(
                                text="Synthesize a clear response from these tool outputs:\n"
                                + "\n".join(tool_outputs)
                            )
                        ],
                    )
                )
                final_resp = client.models.generate_content(
                    model=self.synthesis_model,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        system_instruction=self.system_prompt,
                        temperature=0.1,
                    ),
                )
                return final_resp.text or "\n".join(tool_outputs)

            return response.text or self._execute_worker_pipeline(state)
        except Exception as exc:
            logger.warning(
                "Gemini API call fallback triggered (%s); using autonomous worker pipeline.",
                exc,
            )
            return self._execute_worker_pipeline(state)

    def reset_session(self, session_id: str = "default_session") -> None:
        """Clear conversational memory for the given session ID."""
        memory_manager.clear_session(session_id)


orchestrator = EnterpriseSupportOrchestrator()

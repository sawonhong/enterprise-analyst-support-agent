"""Production ADK tools for customer support data analysis and stateful ticket automation."""

import csv
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from enterprise_support_agent.async_worker import async_worker
from enterprise_support_agent.config import settings
from enterprise_support_agent.schemas import (
    ANALYZE_CUSTOMER_DATA_SCHEMA,
    ESCALATE_OR_REPLY_TICKET_SCHEMA,
    EXECUTIVE_DIGEST_SCHEMA,
    TICKET_STATUS_LOOKUP_SCHEMA,
    AnalyzeCustomerDataInput,
    AnalyzeCustomerDataOutput,
    EscalateOrReplyTicketInput,
    EscalateOrReplyTicketOutput,
    ExecutiveDigestInput,
    TicketStatusLookupInput,
    ValidationError,
)
from enterprise_support_agent.storage import storage
from enterprise_support_agent.telemetry import PIIRedactor, tracer
from enterprise_support_agent.webhook_client import webhook_client

try:
    import pandas as pd  # type: ignore

    HAS_PANDAS = True
except ImportError:
    pd = None
    HAS_PANDAS = False

DEFAULT_TIER_SLA_HOURS: Dict[str, float] = {
    "Enterprise": 4.0,
    "Business": 8.0,
    "Pro": 12.0,
    "Free": 24.0,
}


def _classify_sentiment(message: str) -> Dict[str, Any]:
    """Compute customer sentiment label and frustration score from inquiry text."""
    if not message or not message.strip():
        return {"sentiment": "neutral", "frustration_score": 0.2}

    lower_msg = message.lower()
    frustrated_markers = (
        "unacceptable",
        "losing $",
        "immediately",
        "fix this now",
        "dispute",
        "double-charged",
        "nobody can access",
        "dropped 80%",
        "!!",
    )
    urgent_markers = (
        "urgent",
        "critical",
        "outage",
        "http 503",
        "failing",
        "spiked",
        "stale data",
        "blocking",
    )
    positive_markers = ("love", "awesome", "great", "thank", "appreciate")

    frustration_hits = sum(1 for token in frustrated_markers if token in lower_msg)
    urgent_hits = sum(1 for token in urgent_markers if token in lower_msg)
    positive_hits = sum(1 for token in positive_markers if token in lower_msg)

    if frustration_hits >= 1 and (urgent_hits >= 1 or "!" in message):
        score = min(1.0, round(0.65 + 0.1 * (frustration_hits + urgent_hits), 2))
        return {"sentiment": "frustrated", "frustration_score": score}
    if urgent_hits >= 1:
        score = min(0.85, round(0.5 + 0.1 * urgent_hits, 2))
        return {"sentiment": "urgent", "frustration_score": score}
    if positive_hits >= 1:
        return {"sentiment": "positive", "frustration_score": 0.05}
    return {"sentiment": "neutral", "frustration_score": 0.25}


def _compute_priority_and_team(
    tier: str,
    category: str,
    sentiment: str,
    sla_status: str,
) -> Dict[str, str]:
    """Determine ticket priority ('P0'-'P3') and specialist queue routing."""
    cat_lower = category.lower()
    if "outage" in cat_lower or "database" in cat_lower or "replication" in cat_lower:
        team = "SRE-Critical-Response"
    elif "security" in cat_lower or "sso" in cat_lower:
        team = "Security-Incident-Team"
    elif "billing" in cat_lower or "invoic" in cat_lower:
        team = "Billing-Operations"
    elif "data" in cat_lower or "export" in cat_lower or "api" in cat_lower:
        team = "Data-Platform-Support"
    else:
        team = "Tier2-Support"

    if tier == "Enterprise":
        if (
            sla_status == "breached"
            or "outage" in cat_lower
            or "security" in cat_lower
            or sentiment == "frustrated"
        ):
            return {"priority": "P0", "recommended_team": team}
        if sla_status == "at_risk" or sentiment == "urgent":
            return {"priority": "P1", "recommended_team": team}
        return {"priority": "P2", "recommended_team": team}

    if tier == "Business":
        if sla_status == "breached" or sentiment == "frustrated":
            return {"priority": "P1", "recommended_team": team}
        return {"priority": "P2", "recommended_team": team}

    if tier == "Pro":
        if sla_status == "breached" or sentiment in ("frustrated", "urgent"):
            return {"priority": "P2", "recommended_team": team}
        return {"priority": "P3", "recommended_team": team}

    return {"priority": "P3", "recommended_team": team}


def _load_and_enrich_records(dataset_path: str) -> Dict[str, Any]:
    """Load raw CSV/JSON records, handle missing-field edge cases, and enrich metrics."""
    path_obj = Path(dataset_path)
    if not path_obj.exists():
        raise FileNotFoundError(f"Dataset file not found at '{dataset_path}'.")

    raw_rows: List[Dict[str, Any]] = []
    if path_obj.suffix.lower() == ".json":
        with open(path_obj, "r", encoding="utf-8") as file_obj:
            loaded = json.load(file_obj)
            raw_rows = loaded if isinstance(loaded, list) else loaded.get("tickets", [])
    elif HAS_PANDAS and pd is not None:
        df = pd.read_csv(path_obj, dtype=str).fillna("")
        raw_rows = df.to_dict(orient="records")
    else:
        with open(path_obj, "r", encoding="utf-8", newline="") as file_obj:
            reader = csv.DictReader(file_obj)
            raw_rows = [
                {k: (v if v is not None else "") for k, v in row.items()}
                for row in reader
            ]

    enriched_tickets: List[Dict[str, Any]] = []
    data_quality_issues: List[Dict[str, Any]] = []

    for idx, row in enumerate(raw_rows):
        ticket_id = str(row.get("ticket_id", "")).strip() or f"TKT-UNKNOWN-{idx + 1}"
        customer_id = str(row.get("customer_id", "")).strip() or "CUST-UNKNOWN"
        customer_tier = str(row.get("customer_tier", "")).strip() or "Free"
        if customer_tier not in DEFAULT_TIER_SLA_HOURS:
            data_quality_issues.append(
                {
                    "ticket_id": ticket_id,
                    "field": "customer_tier",
                    "issue": f"Unrecognized tier '{customer_tier}'; defaulted to 'Free'.",
                }
            )
            customer_tier = "Free"

        issue_category = str(row.get("issue_category", "")).strip()
        if not issue_category:
            issue_category = "Uncategorized Inquiry"
            data_quality_issues.append(
                {
                    "ticket_id": ticket_id,
                    "field": "issue_category",
                    "issue": "Missing issue_category; imputed as 'Uncategorized Inquiry'.",
                }
            )

        raw_message = str(row.get("message", "")).strip()
        if not raw_message:
            raw_message = "[No inquiry message provided by customer]"
            data_quality_issues.append(
                {
                    "ticket_id": ticket_id,
                    "field": "message",
                    "issue": "Missing customer inquiry message; flagged for follow-up.",
                }
            )

        raw_elapsed = str(row.get("elapsed_hours", "")).strip()
        try:
            elapsed_hours = float(raw_elapsed) if raw_elapsed else 0.0
        except ValueError:
            elapsed_hours = 0.0
            data_quality_issues.append(
                {
                    "ticket_id": ticket_id,
                    "field": "elapsed_hours",
                    "issue": f"Malformed elapsed_hours '{raw_elapsed}'; defaulted to 0.0.",
                }
            )

        raw_sla = str(row.get("sla_hours", "")).strip()
        try:
            sla_hours = (
                float(raw_sla)
                if raw_sla
                else DEFAULT_TIER_SLA_HOURS.get(customer_tier, 24.0)
            )
            if not raw_sla:
                data_quality_issues.append(
                    {
                        "ticket_id": ticket_id,
                        "field": "sla_hours",
                        "issue": (
                            f"Missing sla_hours; imputed {sla_hours}h from "
                            f"'{customer_tier}' tier policy."
                        ),
                    }
                )
        except ValueError:
            sla_hours = DEFAULT_TIER_SLA_HOURS.get(customer_tier, 24.0)
            data_quality_issues.append(
                {
                    "ticket_id": ticket_id,
                    "field": "sla_hours",
                    "issue": f"Malformed sla_hours '{raw_sla}'; imputed {sla_hours}h.",
                }
            )

        sla_utilization_pct = round((elapsed_hours / max(sla_hours, 0.1)) * 100.0, 1)
        if elapsed_hours > sla_hours:
            sla_status = "breached"
        elif elapsed_hours >= 0.75 * sla_hours:
            sla_status = "at_risk"
        else:
            sla_status = "within_sla"

        sentiment_info = _classify_sentiment(raw_message)
        sentiment = sentiment_info["sentiment"]
        frustration_score = sentiment_info["frustration_score"]

        routing_info = _compute_priority_and_team(
            tier=customer_tier,
            category=issue_category,
            sentiment=sentiment,
            sla_status=sla_status,
        )

        # Overlay live persisted state if ticket was already mutated in SQLite
        persisted = storage.get_ticket_state(ticket_id)
        current_status = (
            persisted["status"]
            if persisted
            else (str(row.get("status", "open")).strip() or "open")
        )
        current_priority = (
            persisted["priority"] if persisted else routing_info["priority"]
        )
        assigned_team = (
            persisted["assigned_team"]
            if persisted
            else routing_info["recommended_team"]
        )

        enriched_tickets.append(
            {
                "ticket_id": ticket_id,
                "customer_id": customer_id,
                "customer_tier": customer_tier,
                "issue_category": issue_category,
                "message": PIIRedactor.redact(raw_message),
                "created_at": str(row.get("created_at", "")).strip(),
                "elapsed_hours": elapsed_hours,
                "sla_hours": sla_hours,
                "sla_utilization_pct": sla_utilization_pct,
                "sla_status": sla_status,
                "sentiment": sentiment,
                "frustration_score": frustration_score,
                "priority": current_priority,
                "assigned_team": assigned_team,
                "status": current_status,
            }
        )

    return {
        "tickets": enriched_tickets,
        "data_quality_issues": data_quality_issues,
    }


def analyze_customer_data(
    dataset_path: Optional[str] = None,
    customer_tier: Optional[str] = None,
    issue_category: Optional[str] = None,
    status_filter: Optional[str] = None,
    ticket_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Analyze customer support inquiries for sentiment, SLA compliance, priority, and anomalies.

    Reads the customer support CSV or JSON dataset, imputes missing fields safely,
    calculates SLA breach/at-risk metrics, classifies customer sentiment, and returns
    a Pydantic-validated analytical summary.

    Args:
        dataset_path (Optional[str]): Path to the CSV/JSON support dataset. Defaults to configured dataset.
        customer_tier (Optional[str]): Filter by tier ('Enterprise', 'Business', 'Pro', 'Free').
        issue_category (Optional[str]): Filter by issue category substring (e.g., 'Outage', 'SSO').
        status_filter (Optional[str]): Filter by ticket status ('open', 'in_progress', 'escalated', 'resolved').
        ticket_id (Optional[str]): Filter for a single ticket ID (e.g., 'TKT-1001').

    Returns:
        Dict[str, Any]: Structured analytical report containing SLA compliance rates,
            sentiment & priority breakdowns, data quality edge cases, and critical tickets.

    Raises:
        ValidationError: Handled internally; returns structured error with `recovery_guidance`.
    """
    raw_args = {
        "dataset_path": dataset_path,
        "customer_tier": customer_tier,
        "issue_category": issue_category,
        "status_filter": status_filter,
        "ticket_id": ticket_id,
    }
    span_ctx = tracer.start_tool_span("analyze_customer_data", raw_args)

    try:
        validated = AnalyzeCustomerDataInput(
            dataset_path=dataset_path,
            customer_tier=customer_tier,
            issue_category=issue_category,
            status_filter=status_filter,
            ticket_id=ticket_id,
        )
        target_path = validated.dataset_path or settings.ticket_dataset_path
        loaded = _load_and_enrich_records(target_path)
        tickets = loaded["tickets"]
        quality_issues = loaded["data_quality_issues"]

        if validated.ticket_id:
            target_id = validated.ticket_id.strip().upper()
            tickets = [t for t in tickets if t["ticket_id"].upper() == target_id]
        if validated.customer_tier:
            tickets = [
                t for t in tickets if t["customer_tier"] == validated.customer_tier
            ]
        if validated.issue_category:
            cat_q = validated.issue_category.strip().lower()
            tickets = [
                t for t in tickets if cat_q in t["issue_category"].lower()
            ]
        if validated.status_filter:
            tickets = [t for t in tickets if t["status"] == validated.status_filter]

        if not tickets:
            recovery = (
                "No tickets matched the provided filters. Try calling "
                "`analyze_customer_data()` without filters to inspect all available "
                "tiers ('Enterprise', 'Business', 'Pro', 'Free') and ticket IDs."
            )
            not_found_payload = {
                "status": "not_found",
                "total_tickets_analyzed": 0,
                "message": "No tickets matched the specified filter criteria.",
                "recovery_guidance": recovery,
            }
            tracer.end_tool_span(
                span_ctx, not_found_payload, recovery_guidance=recovery
            )
            return not_found_payload

        total = len(tickets)
        breached_count = sum(1 for t in tickets if t["sla_status"] == "breached")
        at_risk_count = sum(1 for t in tickets if t["sla_status"] == "at_risk")
        within_sla_count = total - breached_count
        compliance_rate = round((within_sla_count / max(total, 1)) * 100.0, 1)

        sentiment_breakdown: Dict[str, int] = {
            "frustrated": 0,
            "urgent": 0,
            "neutral": 0,
            "positive": 0,
        }
        priority_breakdown: Dict[str, int] = {"P0": 0, "P1": 0, "P2": 0, "P3": 0}

        for item in tickets:
            sent = item["sentiment"]
            sentiment_breakdown[sent] = sentiment_breakdown.get(sent, 0) + 1
            prio = item["priority"]
            priority_breakdown[prio] = priority_breakdown.get(prio, 0) + 1

        critical_tickets = [
            t
            for t in tickets
            if t["priority"] in ("P0", "P1") or t["sla_status"] in ("breached", "at_risk")
        ]
        critical_tickets.sort(
            key=lambda x: (
                0 if x["priority"] == "P0" else (1 if x["priority"] == "P1" else 2),
                -x["sla_utilization_pct"],
            )
        )

        filtered_ids = {t["ticket_id"] for t in tickets}
        relevant_quality_issues = [
            q for q in quality_issues if q["ticket_id"] in filtered_ids
        ]

        output_model = AnalyzeCustomerDataOutput(
            status="success",
            total_tickets_analyzed=total,
            sla_compliance_rate_pct=compliance_rate,
            sla_breached_count=breached_count,
            sla_at_risk_count=at_risk_count,
            sentiment_breakdown=sentiment_breakdown,
            priority_breakdown=priority_breakdown,
            data_quality_issues=relevant_quality_issues,
            critical_tickets=critical_tickets,
            tickets=tickets,
        )
        result_dict = output_model.model_dump()
        tracer.end_tool_span(span_ctx, result_dict)
        return result_dict

    except ValidationError as val_err:
        recovery = (
            "Invalid filter parameter supplied to `analyze_customer_data`. "
            "Ensure `customer_tier` is one of ('Enterprise', 'Business', 'Pro', 'Free') "
            "and `status_filter` is one of ('open', 'in_progress', 'escalated', 'resolved', 'closed')."
        )
        err_payload = {
            "status": "error",
            "error_type": "ValidationError",
            "message": str(val_err),
            "recovery_guidance": recovery,
        }
        tracer.end_tool_span(
            span_ctx, err_payload, error=val_err, recovery_guidance=recovery
        )
        return err_payload
    except FileNotFoundError as fnf_err:
        recovery = (
            f"Dataset path '{dataset_path}' does not exist. Omit `dataset_path` to use "
            f"the default dataset at '{settings.ticket_dataset_path}'."
        )
        err_payload = {
            "status": "error",
            "error_type": "FileNotFoundError",
            "message": str(fnf_err),
            "recovery_guidance": recovery,
        }
        tracer.end_tool_span(
            span_ctx, err_payload, error=fnf_err, recovery_guidance=recovery
        )
        return err_payload
    except Exception as exc:
        recovery = (
            "Unexpected error while analyzing customer dataset. Verify CSV formatting "
            "and retry with default parameters."
        )
        err_payload = {
            "status": "error",
            "error_type": type(exc).__name__,
            "message": str(exc),
            "recovery_guidance": recovery,
        }
        tracer.end_tool_span(
            span_ctx, err_payload, error=exc, recovery_guidance=recovery
        )
        return err_payload


def escalate_or_reply_ticket(
    ticket_id: str,
    action_type: str = "triage_and_reply",
    priority: Optional[str] = None,
    custom_reply_note: Optional[str] = None,
    on_call_team: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute stateful ticket escalation, priority assignment, customer reply drafting, and webhook dispatch.

    Looks up the target ticket, computes or overrides its priority and specialist routing
    queue, drafts a tailored customer response, persists the state transition in SQLite,
    dispatches a signed REST webhook event, and triggers async background audit consolidation.

    Args:
        ticket_id (str): Unique ticket identifier matching 'TKT-XXXX' (e.g., 'TKT-1001').
        action_type (str): Action to perform. Must strictly be one of:
            'escalate', 'reply', 'triage_and_reply', or 'resolve'.
        priority (Optional[str]): Optional priority override ('P0', 'P1', 'P2', or 'P3').
        custom_reply_note (Optional[str]): Optional engineering note or remediation update.
        on_call_team (Optional[str]): Optional specialist team override (e.g., 'SRE-Critical-Response').

    Returns:
        Dict[str, Any]: Validated mutation record containing new status, assigned priority,
            drafted customer reply, and REST webhook delivery metadata.

    Raises:
        ValidationError: Handled internally; returns structured error with `recovery_guidance`.
    """
    raw_args = {
        "ticket_id": ticket_id,
        "action_type": action_type,
        "priority": priority,
        "custom_reply_note": custom_reply_note,
        "on_call_team": on_call_team,
    }
    span_ctx = tracer.start_tool_span("escalate_or_reply_ticket", raw_args)

    try:
        normalized_id = ticket_id.strip().upper() if isinstance(ticket_id, str) else ""
        validated = EscalateOrReplyTicketInput(
            ticket_id=normalized_id,
            action_type=action_type,
            priority=priority,
            custom_reply_note=custom_reply_note,
            on_call_team=on_call_team,
        )

        loaded = _load_and_enrich_records(settings.ticket_dataset_path)
        all_tickets = loaded["tickets"]
        matched = [t for t in all_tickets if t["ticket_id"] == validated.ticket_id]

        if not matched:
            available_ids = ", ".join(t["ticket_id"] for t in all_tickets)
            recovery = (
                f"Ticket '{validated.ticket_id}' was not found in the support dataset. "
                f"Available ticket IDs are: {available_ids}. Call `analyze_customer_data()` "
                "first to verify the exact `ticket_id`."
            )
            not_found_payload = {
                "status": "not_found",
                "ticket_id": validated.ticket_id,
                "message": f"Ticket '{validated.ticket_id}' does not exist.",
                "recovery_guidance": recovery,
            }
            tracer.end_tool_span(
                span_ctx, not_found_payload, recovery_guidance=recovery
            )
            return not_found_payload

        ticket = matched[0]
        previous_status = ticket["status"]
        assigned_priority = validated.priority or ticket["priority"]
        assigned_team = validated.on_call_team or ticket["assigned_team"]

        if validated.action_type == "escalate":
            new_status = "escalated"
            escalation_flag = True
        elif validated.action_type == "resolve":
            new_status = "resolved"
            escalation_flag = False
        elif validated.action_type == "triage_and_reply":
            escalation_flag = assigned_priority in ("P0", "P1") or (
                ticket["sla_status"] == "breached"
            )
            new_status = "escalated" if escalation_flag else "in_progress"
        else:
            escalation_flag = assigned_priority == "P0"
            new_status = "in_progress"

        sla_badge = (
            f"SLA BREACHED ({ticket['elapsed_hours']}h / {ticket['sla_hours']}h)"
            if ticket["sla_status"] == "breached"
            else (
                f"SLA AT RISK ({ticket['elapsed_hours']}h / {ticket['sla_hours']}h)"
                if ticket["sla_status"] == "at_risk"
                else f"Within SLA ({ticket['elapsed_hours']}h / {ticket['sla_hours']}h)"
            )
        )

        custom_section = (
            f" Engineering Update: {PIIRedactor.redact(validated.custom_reply_note)}"
            if validated.custom_reply_note
            else ""
        )

        if ticket["issue_category"] == "Uncategorized Inquiry":
            drafted_reply = (
                f"Hello {ticket['customer_id']} ({ticket['customer_tier']} Support Tier), "
                f"we received ticket {ticket['ticket_id']}, but the issue description and category "
                f"were blank. Because you are an {ticket['customer_tier']} customer ({sla_badge}), "
                f"we have assigned priority {assigned_priority} with {assigned_team}. "
                f"Please reply with the affected service and error logs so we can assist immediately.{custom_section}"
            )
        elif escalation_flag:
            drafted_reply = (
                f"Hello {ticket['customer_id']} ({ticket['customer_tier']} Priority Support), "
                f"we have escalated ticket {ticket['ticket_id']} regarding '{ticket['issue_category']}' "
                f"to {assigned_priority} Incident Response with our {assigned_team} squad ({sla_badge}). "
                f"An incident commander is actively investigating and will provide updates every 30 minutes.{custom_section}"
            )
        else:
            drafted_reply = (
                f"Hello {ticket['customer_id']} ({ticket['customer_tier']} Support), "
                f"thank you for contacting us regarding '{ticket['issue_category']}' (Ticket {ticket['ticket_id']}). "
                f"Your inquiry has been triaged as {assigned_priority} and assigned to {assigned_team} ({sla_badge}). "
                f"Here are the next steps for your team.{custom_section}"
            )

        webhook_event_type = (
            "ticket.escalated"
            if escalation_flag
            else (
                "ticket.resolved"
                if new_status == "resolved"
                else "ticket.replied"
            )
        )
        webhook_delivery = webhook_client.dispatch_ticket_event(
            ticket_id=validated.ticket_id,
            event_type=webhook_event_type,
            priority=assigned_priority,
            assigned_team=assigned_team,
            payload={
                "ticket_id": validated.ticket_id,
                "customer_id": ticket["customer_id"],
                "customer_tier": ticket["customer_tier"],
                "issue_category": ticket["issue_category"],
                "previous_status": previous_status,
                "new_status": new_status,
                "priority": assigned_priority,
                "assigned_team": assigned_team,
                "sla_status": ticket["sla_status"],
                "escalation_flag": escalation_flag,
                "drafted_customer_reply": drafted_reply,
            },
        )

        storage.upsert_ticket_mutation(
            ticket_id=validated.ticket_id,
            customer_id=ticket["customer_id"],
            customer_tier=ticket["customer_tier"],
            issue_category=ticket["issue_category"],
            previous_status=previous_status,
            new_status=new_status,
            priority=assigned_priority,
            assigned_team=assigned_team,
            escalation_flag=escalation_flag,
            sla_status=ticket["sla_status"],
            sentiment=ticket["sentiment"],
            drafted_reply=drafted_reply,
            webhook_event_id=webhook_delivery["event_id"],
            action_type=validated.action_type,
            notes=validated.custom_reply_note,
        )

        # Dispatch non-blocking background audit consolidation
        async_worker.submit_audit_consolidation(
            ticket_id=validated.ticket_id,
            action_type=validated.action_type,
            summary_notes=(
                f"Transitioned {validated.ticket_id} from {previous_status} to {new_status} "
                f"({assigned_priority} -> {assigned_team})."
            ),
        )

        output_model = EscalateOrReplyTicketOutput(
            status="success",
            ticket_id=validated.ticket_id,
            previous_status=previous_status,
            new_status=new_status,
            assigned_priority=assigned_priority,
            assigned_team=assigned_team,
            escalation_flag=escalation_flag,
            drafted_customer_reply=drafted_reply,
            webhook_delivery=webhook_delivery,
        )
        result_dict = output_model.model_dump()
        tracer.end_tool_span(span_ctx, result_dict)
        return result_dict

    except ValidationError as val_err:
        recovery = (
            "Invalid parameters for `escalate_or_reply_ticket`. "
            "Ensure `ticket_id` matches 'TKT-XXXX' (e.g., 'TKT-1001'), "
            "`action_type` is one of ('escalate', 'reply', 'triage_and_reply', 'resolve'), "
            "and `priority` (if provided) is one of ('P0', 'P1', 'P2', 'P3')."
        )
        err_payload = {
            "status": "error",
            "error_type": "ValidationError",
            "message": str(val_err),
            "recovery_guidance": recovery,
        }
        tracer.end_tool_span(
            span_ctx, err_payload, error=val_err, recovery_guidance=recovery
        )
        return err_payload
    except Exception as exc:
        recovery = (
            "Unexpected error mutating ticket state. Verify SQLite database permissions "
            "and confirm the ticket ID exists via `analyze_customer_data`."
        )
        err_payload = {
            "status": "error",
            "error_type": type(exc).__name__,
            "message": str(exc),
            "recovery_guidance": recovery,
        }
        tracer.end_tool_span(
            span_ctx, err_payload, error=exc, recovery_guidance=recovery
        )
        return err_payload


def get_ticket_history_and_status(
    ticket_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Retrieve live stateful ticket status, drafted replies, and SQLite audit history.

    Args:
        ticket_id (Optional[str]): Specific ticket ID ('TKT-XXXX') to inspect.
            If None, returns all mutated tickets in the SQLite store.

    Returns:
        Dict[str, Any]: Persisted ticket state(s), audit log trail, and webhook IDs.

    Raises:
        ValidationError: Handled internally; returns structured error with `recovery_guidance`.
    """
    raw_args = {"ticket_id": ticket_id}
    span_ctx = tracer.start_tool_span("get_ticket_history_and_status", raw_args)

    try:
        normalized_id = ticket_id.strip().upper() if ticket_id else None
        validated = TicketStatusLookupInput(ticket_id=normalized_id)

        if validated.ticket_id:
            persisted = storage.get_ticket_state(validated.ticket_id)
            if persisted:
                result = {"status": "success", "ticket": persisted}
                tracer.end_tool_span(span_ctx, result)
                return result

            # Fallback to checking unmutated dataset state
            analysis = analyze_customer_data(ticket_id=validated.ticket_id)
            if analysis.get("status") == "success" and analysis.get("tickets"):
                ticket_obj = analysis["tickets"][0]
                result = {
                    "status": "success",
                    "ticket": {
                        **ticket_obj,
                        "escalation_flag": ticket_obj["priority"] == "P0",
                        "drafted_reply": None,
                        "audit_trail": [],
                    },
                }
                tracer.end_tool_span(span_ctx, result)
                return result

            recovery = (
                f"Ticket '{validated.ticket_id}' was not found in SQLite or the raw dataset. "
                "Call `analyze_customer_data()` to list valid ticket IDs."
            )
            err_payload = {
                "status": "not_found",
                "ticket_id": validated.ticket_id,
                "message": f"No state found for ticket '{validated.ticket_id}'.",
                "recovery_guidance": recovery,
            }
            tracer.end_tool_span(span_ctx, err_payload, recovery_guidance=recovery)
            return err_payload

        all_states = storage.get_all_ticket_states()
        result = {
            "status": "success",
            "total_mutated_tickets": len(all_states),
            "tickets": all_states,
        }
        tracer.end_tool_span(span_ctx, result)
        return result

    except ValidationError as val_err:
        recovery = "Provide a valid `ticket_id` matching 'TKT-XXXX' (e.g., 'TKT-1001') or omit it."
        err_payload = {
            "status": "error",
            "error_type": "ValidationError",
            "message": str(val_err),
            "recovery_guidance": recovery,
        }
        tracer.end_tool_span(
            span_ctx, err_payload, error=val_err, recovery_guidance=recovery
        )
        return err_payload


def generate_sla_executive_digest(
    include_recommendations: bool = True,
) -> Dict[str, Any]:
    """Generate a comprehensive executive SLA & customer sentiment analytical digest.

    Args:
        include_recommendations (bool): Whether to include automated staffing and
            engineering action recommendations. Defaults to True.

    Returns:
        Dict[str, Any]: Executive digest containing Markdown report, key metrics,
            and prioritized action items.
    """
    raw_args = {"include_recommendations": include_recommendations}
    span_ctx = tracer.start_tool_span("generate_sla_executive_digest", raw_args)

    try:
        validated = ExecutiveDigestInput(
            include_recommendations=include_recommendations
        )
        analysis = analyze_customer_data()
        if analysis.get("status") != "success":
            tracer.end_tool_span(span_ctx, analysis)
            return analysis

        total = analysis["total_tickets_analyzed"]
        compliance = analysis["sla_compliance_rate_pct"]
        breached = analysis["sla_breached_count"]
        at_risk = analysis["sla_at_risk_count"]
        prio = analysis["priority_breakdown"]
        sent = analysis["sentiment_breakdown"]
        anomalies = analysis["data_quality_issues"]
        critical = analysis["critical_tickets"]

        recommendations: List[str] = []
        if validated.include_recommendations:
            if breached > 0:
                recommendations.append(
                    f"Immediate Action: Page SRE & Security on-call for {breached} SLA-breached ticket(s)."
                )
            if at_risk > 0:
                recommendations.append(
                    f"Proactive Intervention: Prioritize {at_risk} SLA at-risk ticket(s) (>=75% elapsed)."
                )
            if anomalies:
                recommendations.append(
                    f"Data Hygiene: Resolve {len(anomalies)} ingestion schema anomaly(ies) (missing fields)."
                )

        critical_lines = "\n".join(
            f"- **{t['ticket_id']}** ({t['customer_tier']} | `{t['priority']}` | `{t['sla_status']}`): "
            f"{t['issue_category']} -> Routed to `{t['assigned_team']}`"
            for t in critical[:5]
        )
        rec_lines = "\n".join(f"- {r}" for r in recommendations)

        markdown_report = (
            "### 📊 Enterprise Customer Support & SLA Executive Digest\n\n"
            f"- **Total Inquiries Analyzed**: {total}\n"
            f"- **Overall SLA Compliance Rate**: {compliance}%\n"
            f"- **SLA Breached**: {breached} | **SLA At-Risk**: {at_risk}\n"
            f"- **Priority Distribution**: P0={prio.get('P0', 0)}, P1={prio.get('P1', 0)}, "
            f"P2={prio.get('P2', 0)}, P3={prio.get('P3', 0)}\n"
            f"- **Sentiment Breakdown**: Frustrated={sent.get('frustrated', 0)}, "
            f"Urgent={sent.get('urgent', 0)}, Neutral={sent.get('neutral', 0)}, "
            f"Positive={sent.get('positive', 0)}\n"
            f"- **Data Quality Anomalies Detected**: {len(anomalies)}\n\n"
            f"#### 🚨 Critical & At-Risk Tickets\n{critical_lines}\n"
        )
        if rec_lines:
            markdown_report += f"\n#### 💡 Recommended Actions\n{rec_lines}\n"

        result = {
            "status": "success",
            "summary_metrics": {
                "total_tickets": total,
                "sla_compliance_rate_pct": compliance,
                "sla_breached_count": breached,
                "sla_at_risk_count": at_risk,
                "priority_breakdown": prio,
                "sentiment_breakdown": sent,
                "data_quality_anomaly_count": len(anomalies),
            },
            "recommendations": recommendations,
            "markdown_report": markdown_report,
        }
        tracer.end_tool_span(span_ctx, result)
        return result
    except Exception as exc:
        recovery = "Failed to synthesize executive digest. Verify dataset availability."
        err_payload = {
            "status": "error",
            "error_type": type(exc).__name__,
            "message": str(exc),
            "recovery_guidance": recovery,
        }
        tracer.end_tool_span(
            span_ctx, err_payload, error=exc, recovery_guidance=recovery
        )
        return err_payload


ALL_TOOLS = [
    analyze_customer_data,
    escalate_or_reply_ticket,
    get_ticket_history_and_status,
    generate_sla_executive_digest,
]

ALL_TOOL_SCHEMAS = {
    "analyze_customer_data": ANALYZE_CUSTOMER_DATA_SCHEMA,
    "escalate_or_reply_ticket": ESCALATE_OR_REPLY_TICKET_SCHEMA,
    "get_ticket_history_and_status": TICKET_STATUS_LOOKUP_SCHEMA,
    "generate_sla_executive_digest": EXECUTIVE_DIGEST_SCHEMA,
}

"""Explicit Pydantic v2 input and output JSON schemas for ADK tools."""

import re
from typing import Any, Dict, List, Optional

try:
    from pydantic import BaseModel, Field, ValidationError  # type: ignore

    HAS_PYDANTIC = True
except ImportError:
    HAS_PYDANTIC = False

    class ValidationError(ValueError):
        """Fallback validation error when pydantic is not installed in the environment."""

    def Field(default: Any = ..., **kwargs: Any) -> Any:  # noqa: N802
        """Compatibility shim for pydantic.Field metadata."""
        return {"__field_spec__": True, "default": default, **kwargs}

    class BaseModel:  # type: ignore
        """Lightweight compatibility implementation of pydantic.BaseModel."""

        def __init__(self, **data: Any) -> None:
            annotations = getattr(self.__class__, "__annotations__", {})
            for field_name in annotations:
                spec = getattr(self.__class__, field_name, None)
                is_spec = isinstance(spec, dict) and spec.get("__field_spec__")
                default_val = spec.get("default") if is_spec else spec

                if field_name in data:
                    val = data[field_name]
                elif default_val is not ...:
                    val = default_val
                else:
                    raise ValidationError(f"Missing required field: '{field_name}'")

                if is_spec and val is not None:
                    min_len = spec.get("min_length")
                    if min_len is not None and isinstance(val, str) and len(val) < min_len:
                        raise ValidationError(
                            f"Field '{field_name}' must be at least {min_len} characters."
                        )
                    pattern = spec.get("pattern")
                    if pattern is not None and isinstance(val, str):
                        if not re.match(pattern, val):
                            raise ValidationError(
                                f"Field '{field_name}' value '{val}' does not match pattern '{pattern}'."
                            )
                setattr(self, field_name, val)

        def model_dump(self) -> Dict[str, Any]:
            """Return dictionary representation of model fields."""
            annotations = getattr(self.__class__, "__annotations__", {})
            return {name: getattr(self, name, None) for name in annotations}

        @classmethod
        def model_json_schema(cls) -> Dict[str, Any]:
            """Generate explicit JSON schema dictionary for LLM tool declarations."""
            annotations = getattr(cls, "__annotations__", {})
            properties: Dict[str, Any] = {}
            required: List[str] = []
            for field_name, field_type in annotations.items():
                spec = getattr(cls, field_name, None)
                is_spec = isinstance(spec, dict) and spec.get("__field_spec__")
                prop_schema: Dict[str, Any] = {"type": "string"}
                type_str = str(field_type)
                if "bool" in type_str:
                    prop_schema["type"] = "boolean"
                elif "int" in type_str:
                    prop_schema["type"] = "integer"
                elif "float" in type_str:
                    prop_schema["type"] = "number"
                elif "List" in type_str or "list" in type_str:
                    prop_schema["type"] = "array"
                elif "Dict" in type_str or "dict" in type_str:
                    prop_schema["type"] = "object"
                if is_spec:
                    if spec.get("description"):
                        prop_schema["description"] = spec["description"]
                    if spec.get("pattern"):
                        prop_schema["pattern"] = spec["pattern"]
                    if spec.get("default") is ...:
                        required.append(field_name)
                    elif spec.get("default") is not None:
                        prop_schema["default"] = spec["default"]
                properties[field_name] = prop_schema
            return {
                "title": cls.__name__,
                "type": "object",
                "properties": properties,
                "required": required,
            }


class AnalyzeCustomerDataInput(BaseModel):
    """Strict input schema for the `analyze_customer_data` ADK tool."""

    dataset_path: Optional[str] = Field(
        default=None,
        description=(
            "Optional file path to the customer support CSV or JSON dataset. "
            "Defaults to the configured enterprise dataset path."
        ),
    )
    customer_tier: Optional[str] = Field(
        default=None,
        pattern="^(Enterprise|Business|Pro|Free)$",
        description=(
            "Optional customer tier filter. Must strictly be one of: "
            "'Enterprise', 'Business', 'Pro', or 'Free'."
        ),
    )
    issue_category: Optional[str] = Field(
        default=None,
        description=(
            "Optional issue category substring filter (e.g., 'Production Outage', "
            "'Security & SSO', 'Billing', 'API Rate Limit')."
        ),
    )
    status_filter: Optional[str] = Field(
        default=None,
        pattern="^(open|in_progress|escalated|resolved|closed)$",
        description=(
            "Optional ticket status filter ('open', 'in_progress', 'escalated', "
            "'resolved', or 'closed')."
        ),
    )
    ticket_id: Optional[str] = Field(
        default=None,
        description="Optional specific ticket ID to inspect (e.g., 'TKT-1001').",
    )


class AnalyzeCustomerDataOutput(BaseModel):
    """Structured output schema returned by `analyze_customer_data`."""

    status: str = Field(..., description="Execution status ('success' or 'error').")
    total_tickets_analyzed: int = Field(
        ..., description="Count of tickets matching the filter criteria."
    )
    sla_compliance_rate_pct: float = Field(
        ..., description="Percentage of analyzed tickets currently within SLA."
    )
    sla_breached_count: int = Field(
        ..., description="Number of tickets that have exceeded their SLA target hours."
    )
    sla_at_risk_count: int = Field(
        ..., description="Number of tickets at >=75% of their SLA target hours."
    )
    sentiment_breakdown: Dict[str, int] = Field(
        ..., description="Ticket counts grouped by sentiment ('frustrated', 'urgent', 'neutral', 'positive')."
    )
    priority_breakdown: Dict[str, int] = Field(
        ..., description="Ticket counts grouped by computed priority ('P0', 'P1', 'P2', 'P3')."
    )
    data_quality_issues: List[Dict[str, Any]] = Field(
        ..., description="List of edge-case data anomalies (missing fields, imputed SLAs)."
    )
    critical_tickets: List[Dict[str, Any]] = Field(
        ..., description="Detailed records of high-priority (P0/P1) or SLA-breached tickets."
    )
    tickets: List[Dict[str, Any]] = Field(
        ..., description="Enriched ticket records with sentiment, SLA status, and priority."
    )


class EscalateOrReplyTicketInput(BaseModel):
    """Strict input schema for the `escalate_or_reply_ticket` ADK tool."""

    ticket_id: str = Field(
        ...,
        min_length=4,
        pattern="^TKT-[0-9]{4,}$",
        description="Unique ticket identifier to mutate (must match pattern 'TKT-XXXX', e.g., 'TKT-1001').",
    )
    action_type: str = Field(
        ...,
        pattern="^(escalate|reply|triage_and_reply|resolve)$",
        description=(
            "Stateful action to execute. Must strictly be one of: "
            "'escalate', 'reply', 'triage_and_reply', or 'resolve'."
        ),
    )
    priority: Optional[str] = Field(
        default=None,
        pattern="^(P0|P1|P2|P3)$",
        description="Optional explicit priority override ('P0', 'P1', 'P2', or 'P3').",
    )
    custom_reply_note: Optional[str] = Field(
        default=None,
        description="Optional custom engineering note or resolution message to include in the customer reply.",
    )
    on_call_team: Optional[str] = Field(
        default=None,
        description=(
            "Optional target specialist queue (e.g., 'SRE-Critical-Response', "
            "'Security-Incident-Team', 'Billing-Operations', 'Tier2-Support')."
        ),
    )


class EscalateOrReplyTicketOutput(BaseModel):
    """Structured output schema returned by `escalate_or_reply_ticket`."""

    status: str = Field(..., description="Mutation status ('success', 'requires_approval', or 'error').")
    ticket_id: str = Field(..., description="Mutated ticket identifier.")
    previous_status: str = Field(..., description="Ticket status before mutation.")
    new_status: str = Field(..., description="Updated ticket status persisted in SQLite.")
    assigned_priority: str = Field(..., description="Assigned priority level ('P0'-'P3').")
    assigned_team: str = Field(..., description="Target engineering or support team.")
    escalation_flag: bool = Field(..., description="True if ticket was flagged for urgent escalation.")
    drafted_customer_reply: str = Field(..., description="Tailored empathetic customer response.")
    webhook_delivery: Dict[str, Any] = Field(
        ..., description="Simulated REST webhook delivery confirmation payload."
    )


class TicketStatusLookupInput(BaseModel):
    """Strict input schema for `get_ticket_history_and_status`."""

    ticket_id: Optional[str] = Field(
        default=None,
        pattern="^TKT-[0-9]{4,}$",
        description="Optional specific ticket ID ('TKT-XXXX') to inspect. If omitted, returns all mutated tickets.",
    )


class ExecutiveDigestInput(BaseModel):
    """Strict input schema for `generate_sla_executive_digest`."""

    include_recommendations: bool = Field(
        default=True,
        description="Whether to include automated staffing and engineering remediation recommendations.",
    )


ANALYZE_CUSTOMER_DATA_SCHEMA = AnalyzeCustomerDataInput.model_json_schema()
ESCALATE_OR_REPLY_TICKET_SCHEMA = EscalateOrReplyTicketInput.model_json_schema()
TICKET_STATUS_LOOKUP_SCHEMA = TicketStatusLookupInput.model_json_schema()
EXECUTIVE_DIGEST_SCHEMA = ExecutiveDigestInput.model_json_schema()

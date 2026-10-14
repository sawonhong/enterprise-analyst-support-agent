"""OpenTelemetry distributed tracing, structured JSON logging, and PII redaction."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import logging
import re
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("enterprise_support_agent.telemetry")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


class PIIRedactor:
    """Detects and masks sensitive PII and credentials in logs, traces, and prompts."""

    PATTERNS = {
        "EMAIL": re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
        "PHONE": re.compile(
            r"(?<!\w)(?:\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b"
        ),
        "API_KEY": re.compile(
            r"\b(?:MOCK_STRIPE_KEY|mock_stripe_key|api_token|secret_key)_[A-Za-z0-9_\-]{4,}\b"
        ),
        "IPV4": re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
    }

    @classmethod
    def redact(cls, text: str) -> str:
        """Mask all matching PII and secret tokens in the input string.

        Args:
            text: Raw text that may contain emails, phone numbers, IPs, or keys.

        Returns:
            Sanitized string with sensitive tokens replaced by [REDACTED_<TYPE>].
        """
        if not isinstance(text, str):
            return text
        sanitized = text
        for label, pattern in cls.PATTERNS.items():
            sanitized = pattern.sub(f"[REDACTED_{label}]", sanitized)
        return sanitized

    @classmethod
    def redact_dict(cls, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Recursively redact sensitive PII across dictionary keys and values.

        Args:
            payload: Dictionary containing arbitrary nested structures.

        Returns:
            Deep-copied dictionary with all string values sanitized.
        """
        if not isinstance(payload, dict):
            return payload
        cleaned: Dict[str, Any] = {}
        for key, value in payload.items():
            if isinstance(value, str):
                cleaned[key] = cls.redact(value)
            elif isinstance(value, dict):
                cleaned[key] = cls.redact_dict(value)
            elif isinstance(value, list):
                cleaned[key] = [
                    cls.redact(item)
                    if isinstance(item, str)
                    else (cls.redact_dict(item) if isinstance(item, dict) else item)
                    for item in value
                ]
            else:
                cleaned[key] = value
        return cleaned


try:
    from opentelemetry import trace  # type: ignore
    from opentelemetry.sdk.trace import TracerProvider  # type: ignore
    from opentelemetry.trace import Status, StatusCode  # type: ignore

    _provider = TracerProvider()
    trace.set_tracer_provider(_provider)
    otel_tracer = trace.get_tracer("enterprise_support_agent", "1.0.0")
    HAS_OTEL = True
except Exception:
    otel_tracer = None
    HAS_OTEL = False


@dataclass
class ToolExecutionTrace:
    """Structured telemetry record for a single ADK tool invocation."""

    tool_name: str
    arguments: Dict[str, Any]
    result_summary: str
    duration_ms: float
    status: str = "success"
    error_message: Optional[str] = None
    recovery_guidance: Optional[str] = None
    span_id: str = ""
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> Dict[str, Any]:
        """Serialize the tool trace into a JSON-compatible dictionary."""
        return {
            "tool_name": self.tool_name,
            "arguments": PIIRedactor.redact_dict(self.arguments),
            "result_summary": PIIRedactor.redact(self.result_summary),
            "duration_ms": self.duration_ms,
            "status": self.status,
            "error_message": self.error_message,
            "recovery_guidance": self.recovery_guidance,
            "span_id": self.span_id,
            "timestamp": self.timestamp,
        }


@dataclass
class TurnTrace:
    """Complete distributed trace record for one multi-agent conversational turn."""

    turn_id: str
    session_id: str
    user_query: str
    routed_intent: str
    selected_worker: str
    thought_process: str
    agent_response: str
    tool_calls: List[ToolExecutionTrace] = field(default_factory=list)
    total_duration_ms: float = 0.0
    token_estimate: int = 0
    trace_id: str = ""
    pii_redacted: bool = True
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> Dict[str, Any]:
        """Convert turn trace into a sanitized dictionary."""
        return {
            "turn_id": self.turn_id,
            "trace_id": self.trace_id or self.turn_id,
            "session_id": self.session_id,
            "user_query": PIIRedactor.redact(self.user_query),
            "routed_intent": self.routed_intent,
            "selected_worker": self.selected_worker,
            "thought_process": PIIRedactor.redact(self.thought_process),
            "agent_response": PIIRedactor.redact(self.agent_response),
            "tool_calls": [tool_call.to_dict() for tool_call in self.tool_calls],
            "total_duration_ms": self.total_duration_ms,
            "token_estimate": self.token_estimate,
            "pii_redacted": self.pii_redacted,
            "created_at": self.created_at,
        }

    def to_json(self) -> str:
        """Serialize the turn trace as a single-line structured JSON log."""
        return json.dumps(self.to_dict(), ensure_ascii=False)


class AgentTelemetryTracer:
    """Manages active OpenTelemetry spans, latency metrics, and structured JSON logs."""

    def __init__(self) -> None:
        self.turn_traces: List[TurnTrace] = []
        self.tool_traces: List[ToolExecutionTrace] = []
        self._active_spans: Dict[str, Any] = {}

    @staticmethod
    def _generate_span_id(seed: str) -> str:
        """Generate a deterministic 16-hex-character OpenTelemetry-compatible span ID."""
        return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]

    def start_tool_span(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Start an OpenTelemetry span and timer for a tool execution.

        Args:
            tool_name: Name of the invoked ADK tool.
            arguments: Raw tool input arguments.

        Returns:
            Span context dictionary containing start_time, span_key, and span_id.
        """
        sanitized_args = PIIRedactor.redact_dict(arguments)
        start_perf = time.perf_counter()
        span_key = f"{tool_name}:{start_perf}"
        span_id = self._generate_span_id(span_key)

        if HAS_OTEL and otel_tracer is not None:
            try:
                span = otel_tracer.start_span(name=f"adk.tool.{tool_name}")
                span.set_attribute("adk.tool.name", tool_name)
                span.set_attribute("adk.tool.arguments", json.dumps(sanitized_args))
                self._active_spans[span_key] = span
                if hasattr(span, "get_span_context"):
                    raw_id = span.get_span_context().span_id
                    if raw_id:
                        span_id = format(raw_id, "016x")
            except Exception:
                pass

        logger.info(
            "%s",
            json.dumps(
                {
                    "event": "tool_invocation_start",
                    "tool_name": tool_name,
                    "span_id": span_id,
                    "arguments": sanitized_args,
                }
            ),
        )
        return {
            "tool_name": tool_name,
            "arguments": sanitized_args,
            "start_perf": start_perf,
            "span_key": span_key,
            "span_id": span_id,
        }

    def end_tool_span(
        self,
        span_ctx: Dict[str, Any],
        result: Any,
        error: Optional[Exception] = None,
        recovery_guidance: Optional[str] = None,
    ) -> ToolExecutionTrace:
        """Close an active tool span and emit a structured JSON telemetry log.

        Args:
            span_ctx: Span context returned by start_tool_span.
            result: Tool return payload.
            error: Optional exception raised or handled during execution.
            recovery_guidance: Optional corrective hint returned to the LLM.

        Returns:
            Completed ToolExecutionTrace instance.
        """
        duration_ms = round((time.perf_counter() - span_ctx["start_perf"]) * 1000, 2)
        tool_name = span_ctx["tool_name"]
        span_key = span_ctx["span_key"]
        span_id = span_ctx["span_id"]
        status = "error" if error is not None else "success"
        if isinstance(result, dict) and result.get("status") in ("error", "not_found"):
            status = result["status"]
            recovery_guidance = recovery_guidance or result.get("recovery_guidance")

        if HAS_OTEL and span_key in self._active_spans:
            try:
                span = self._active_spans.pop(span_key)
                span.set_attribute("adk.tool.duration_ms", duration_ms)
                span.set_attribute("adk.tool.status", status)
                if error is not None:
                    span.record_exception(error)
                    span.set_status(Status(StatusCode.ERROR, str(error)))
                else:
                    span.set_status(Status(StatusCode.OK))
                span.end()
            except Exception:
                pass

        summary_str = PIIRedactor.redact(str(result)[:240]) if result is not None else ""
        trace_entry = ToolExecutionTrace(
            tool_name=tool_name,
            arguments=span_ctx["arguments"],
            result_summary=summary_str,
            duration_ms=duration_ms,
            status=status,
            error_message=str(error) if error else None,
            recovery_guidance=recovery_guidance,
            span_id=span_id,
        )
        self.tool_traces.append(trace_entry)

        logger.info(
            "%s",
            json.dumps(
                {
                    "event": "tool_invocation_complete",
                    "tool_name": tool_name,
                    "span_id": span_id,
                    "status": status,
                    "duration_ms": duration_ms,
                    "recovery_guidance": recovery_guidance,
                }
            ),
        )
        return trace_entry

    def record_turn(self, turn_trace: TurnTrace) -> None:
        """Persist a completed conversational turn trace and emit structured JSON."""
        if not turn_trace.trace_id:
            turn_trace.trace_id = hashlib.sha256(
                turn_trace.turn_id.encode("utf-8")
            ).hexdigest()[:32]
        self.turn_traces.append(turn_trace)
        logger.info("%s", turn_trace.to_json())


tracer = AgentTelemetryTracer()

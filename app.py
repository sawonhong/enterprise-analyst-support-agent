"""Streamlit Web UI for the Enterprise Customer Support & Data Analysis Agent."""

from enterprise_support_agent.agent import orchestrator
from enterprise_support_agent.telemetry import tracer
from enterprise_support_agent.tools import (
    analyze_customer_data,
    get_ticket_history_and_status,
)


def main() -> None:
    """Render the Streamlit Operations & Triage Dashboard."""
    import streamlit as st  # type: ignore

    st.set_page_config(
        page_title="Enterprise Support & Analytics Agent (ADK)",
        page_icon="🛡️",
        layout="wide",
    )
    st.title("🛡️ Enterprise Customer Support & Data Analysis Agent (Python ADK)")
    st.caption(
        "Multi-Agent Supervisor-Worker Topology • OpenTelemetry Tracing • Stateful SQLite & Webhook Integration"
    )

    if "session_id" not in st.session_state:
        st.session_state.session_id = "streamlit_ui_session"
    if "messages" not in st.session_state:
        st.session_state.messages = []

    with st.sidebar:
        st.header("📊 Live SLA Snapshot")
        metrics = analyze_customer_data()
        if metrics.get("status") == "success":
            st.metric("Total Tickets", metrics["total_tickets_analyzed"])
            st.metric("SLA Compliance Rate", f"{metrics['sla_compliance_rate_pct']}%")
            st.metric("SLA Breached", metrics["sla_breached_count"])
            st.metric("SLA At-Risk", metrics["sla_at_risk_count"])

        if st.button("🔄 Reset Conversation Memory"):
            orchestrator.reset_session(st.session_state.session_id)
            st.session_state.messages = []
            st.rerun()

    tab_chat, tab_state, tab_telemetry = st.tabs(
        ["💬 Multi-Turn Support Agent", "🗂️ SQLite Ticket State", "🔍 OpenTelemetry Traces"]
    )

    with tab_chat:
        for msg in st.session_state.messages:
            with st.chat_message(msg["role"]):
                st.markdown(msg["content"])

        user_prompt = st.chat_input(
            "Ask to analyze tickets, escalate a P0 outage, or generate an executive SLA digest..."
        )
        if user_prompt:
            st.session_state.messages.append({"role": "user", "content": user_prompt})
            with st.chat_message("user"):
                st.markdown(user_prompt)

            turn = orchestrator.chat(
                user_prompt, session_id=st.session_state.session_id
            )
            with st.chat_message("assistant"):
                st.markdown(turn["response"])
            st.session_state.messages.append(
                {"role": "assistant", "content": turn["response"]}
            )

    with tab_state:
        st.subheader("Persisted Ticket States (SQLite)")
        state_data = get_ticket_history_and_status()
        st.json(state_data)

    with tab_telemetry:
        st.subheader("OpenTelemetry Distributed Turn Traces (PII Redacted)")
        st.json([t.to_dict() for t in tracer.turn_traces])


if __name__ == "__main__":
    main()

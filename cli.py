"""Interactive Command-Line Interface (CLI) for the Enterprise Support Agent."""

import sys

from enterprise_support_agent.agent import orchestrator


def run_interactive_cli() -> None:
    """Launch an interactive multi-turn troubleshooting session in the terminal."""
    session_id = "cli_interactive_session"
    print("=" * 76)
    print("🚀 Enterprise Customer Support & Data Analysis Agent (Python ADK)")
    print("Type 'exit' or 'quit' to end the session, or 'reset' to clear memory.")
    print("=" * 76)

    while True:
        try:
            user_input = input("\n[Operator] > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nExiting CLI session.")
            break

        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit"):
            print("Session terminated.")
            break
        if user_input.lower() == "reset":
            orchestrator.reset_session(session_id)
            print("🔄 Session memory cleared.")
            continue

        turn_result = orchestrator.chat(user_input, session_id=session_id)
        print(f"\n[Agent ({turn_result['agent_state']['routed_intent']})] >")
        print(turn_result["response"])
        print(
            f"\n⏱️  Latency: {turn_result['total_duration_ms']}ms | "
            f"Active Ticket Context: {turn_result['agent_state']['active_ticket_id']}"
        )


if __name__ == "__main__":
    if len(sys.argv) > 1:
        single_query = " ".join(sys.argv[1:])
        res = orchestrator.chat(single_query, session_id="cli_oneshot")
        print(res["response"])
    else:
        run_interactive_cli()

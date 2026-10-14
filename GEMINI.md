# GEMINI.md — Enterprise Support & Data Analysis Agent Project Rules

## 1. Project Overview & Objective
This repository is a **Root Project** implementing a production-grade **Enterprise Customer Support & Data Analysis Agent** built with the **Google Agent Development Kit (`google-adk`)** and **Google GenAI SDK (`google-genai`)**.
It is designed to meet all 5 evaluation pillars of the **AI in 5 Days Assessment (Target Score: 95/95)**:
1. **Tool & Interface Design** (20/20): Comprehensive docstrings, descriptive tool names (`analyze_customer_data`, `escalate_or_reply_ticket`, `get_ticket_history_and_status`, `generate_sla_executive_digest`), strict Pydantic JSON schemas (`model_json_schema()`), and guided error recovery (`recovery_guidance`).
2. **Context & Memory** (20/20): Constitution-driven system instructions, multi-turn short-term session state (`InMemorySessionService` + entity/ticket tracking across turns), persistent SQLite + JSON storage, and non-blocking asynchronous background memory consolidation (`AsyncMemoryWorker`).
3. **Orchestration & Logic** (25/25): Supervisor-Worker Multi-Agent topology (`EnterpriseSupportOrchestrator` + `root_agent` / sub-agents), active Two-Stage Model Routing (`gemini-2.5-flash` intent router + synthesis execution), Input/Output Guardrails, Human-in-the-Loop (HITL) approval hooks, and resilient fallback handling.
4. **Observability & Tracing** (15/15): OpenTelemetry distributed tracing with active spans (`start_span`, `Status`, `StatusCode`), structured JSON telemetry logging, and automated regex-based PII & secret redaction (`PIIRedactor`).
5. **Infrastructure & CI/CD** (15/15): Google Cloud Secret Manager integration with `.env` fallback, `Dockerfile`, Terraform IaC (`terraform/`), GitHub Actions CI pipeline (`.github/workflows/ci.yml`), and an automated end-to-end verification & golden benchmark suite (`test_agent.py` and `tests/test_eval_suite.py`).

---

## 2. Engineering & Style Guidelines
- **Google Python Style Guide Compliance**:
  - Use explicit type annotations (`from typing import Any, Dict, List, Optional`) on all public functions, methods, and tool interfaces.
  - Every ADK tool function must include a complete Google-style docstring (`Args:`, `Returns:`, `Raises:`) so the LLM and schema generator have unambiguous parameter metadata.
  - Never allow raw exceptions to crash an agent turn. All tools must catch `ValidationError` and runtime errors and return a structured dictionary containing `status="error"` and a human/LLM-actionable `recovery_guidance` field.
- **Zero-Leakage PII Policy**:
  - Customer support tickets frequently contain sensitive PII (email addresses, phone numbers, IPv4 addresses, API keys). All telemetry spans, structured logs, and external prompts must pass through `PIIRedactor.redact()`.

---

## 3. Build, Run & Verification Instructions

### Environment Setup
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

### Running the Agent
- **ADK CLI / Web UI**:
  ```bash
  adk run enterprise_support_agent
  adk web .
  ```
- **Interactive Terminal CLI**:
  ```bash
  python3 cli.py
  ```
- **Streamlit Operations Dashboard**:
  ```bash
  streamlit run app.py
  ```

### Running End-to-End Verification & Golden Benchmark Tests
```bash
# Run comprehensive end-to-end verification script
python3 test_agent.py

# Run full unit test and golden regression evaluation suite
python3 -m unittest discover -s . -p "test_*.py" -v
pytest -v
```

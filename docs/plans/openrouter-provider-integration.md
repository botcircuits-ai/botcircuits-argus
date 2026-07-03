# OpenRouter Provider Integration — Implementation Plan

## 1. Executive Summary
Add OpenRouter as a fourth supported LLM provider throughout botcircuits' provider/runtime stack, so any workflow agent (or the CLI/manager UI) can pin to `provider: "openrouter"` with a `vendor/model` id (e.g. `anthropic/claude-3.7-sonnet`, `deepseek/deepseek-chat`), giving access to non-native model vendors without adding a bespoke SDK. Because OpenRouter exposes an OpenAI-compatible **Responses API** (beta) at `base_url=https://openrouter.ai/api/v1`, and `OpenAIProvider` already accepts a `base_url` override, this is primarily a **registration** task across five call sites rather than new provider logic — with one verification spike to confirm tool-calling/streaming parity holds for the specific models this project cares about.

## 2. Task Breakdown

### Backend (Python)
- `src/botcircuits/providers/openrouter.py` *(new)* — thin subclass/wrapper of `OpenAIProvider`, pinning `base_url="https://openrouter.ai/api/v1"`, reading `OPENROUTER_API_KEY`, defaulting model to `os.getenv("OPENROUTER_MODEL", "openai/gpt-4.1")`. Sets recommended OpenRouter attribution headers (`HTTP-Referer`, `X-Title`) via the `AsyncOpenAI` client's `default_headers`.
- `src/botcircuits/providers/__init__.py` — register `"openrouter"` in `make_provider()` and `__all__`.
- `src/botcircuits/cli/setup.py` — add an `"openrouter"` entry to `PROVIDER_CATALOG` (label, `env_var="OPENROUTER_API_KEY"`, `api_key_url="https://openrouter.ai/keys"`, a curated model shortlist).
- `src/botcircuits/cli/app.py` — add `"openrouter"` to the `--provider` choices and its `make_provider()` mirror (this file duplicates the mapping from `providers/__init__.py` — update both, see Risks).
- `src/botcircuits/agent/workflow/workflow_validator.py` — add `"openrouter"` to `_VALID_PROVIDERS` so `agents.<name>.provider: "openrouter"` passes static validation.

### Frontend (Manager UI)
- `manager_web/src/components/StepPanel.tsx` — add `"openrouter"` to `FALLBACK_PROVIDERS` (the catalog-driven path already works once `/api/models` returns it).

### Docs
- `DEVELOPMENT.md` — extend the provider list mention (added in `bc76be1`) to include OpenRouter and its env var.

### Verification spike (do this first — it gates the approach)
- Confirm `AsyncOpenAI(base_url="https://openrouter.ai/api/v1").responses.create(...)` round-trips correctly for at least one Anthropic-hosted and one non-Anthropic-hosted OpenRouter model, including a tool-call turn (function calling is what botcircuits' agent loop depends on most). If the Responses API beta doesn't support tool calling reliably for the target models, fall back to Chat Completions (`client.chat.completions.create`) instead — this would mean **not** subclassing `OpenAIProvider` as-is, but writing a small independent adapter reusing its `_msgs_to_input` / tool-schema shaping logic against the Chat Completions message/tool format instead.

## 3. Acceptance Criteria
- `make_provider("openrouter", None)` returns a working provider that completes a real round-trip against `OPENROUTER_API_KEY`.
- A workflow `agents.<name>` entry `{"provider": "openrouter", "model": "<vendor/model>"}` runs a segment successfully end-to-end through `NativeRuntime` (mirrors `tests/test_runtime_native.py`).
- `GET /api/models` includes an `openrouter` key with `{label, models}`.
- `workflow_validator.static_issues()` does **not** flag `provider: "openrouter"`.
- Tool-calling (function calls) works through the OpenRouter path for at least one non-OpenAI-hosted model — verified by an integration-style test or manual check, not just a mock.
- Existing anthropic/openai/gemini provider and runtime tests remain green; no regression in `tests/test_runtime_native.py`, `tests/test_workflow_validator.py`, `tests/test_manager_backend.py`.

## 4. Technical Approach
- Reuse, don't reinvent: `OpenRouterProvider(OpenAIProvider)` only overrides `__init__` (base_url + api_key + model default) if the verification spike confirms Responses-API compatibility. `name = "openrouter"` for usage/logging.
- Usage accounting: `record_usage()` already lives on the base `LLMProvider` — no changes needed there as long as OpenRouter's `usage` fields on the response match the shape `_normalize()` expects (`input_tokens`, `output_tokens`, `input_tokens_details.cached_tokens`); confirm during the spike since OpenRouter aggregates across vendors and may report usage slightly differently per underlying model.
- No new dependency: reuses the existing `openai` SDK, just pointed at a different `base_url`.
- Keep `workflow_validator.py`'s `_VALID_PROVIDERS` as a hand-maintained literal (per its existing comment) rather than importing from `providers/` — consistent with current design intent to keep the validator decoupled from the runtime layer.

## 5. Dependencies and Risks
- **Responses-API beta maturity**: OpenRouter's docs mark this endpoint "Beta" — tool-calling/streaming parity across arbitrary vendor models isn't guaranteed the way it is for OpenAI's own models. This is the main technical risk and is why the spike is sequenced first. ([OpenRouter Responses API Beta docs](https://openrouter.ai/docs/api/reference/responses/overview))
- **Per-model quirks**: OpenRouter fans out to many backends; not every routed model supports every OpenAI-shaped feature (e.g. hosted `code_interpreter`, strict function-calling schemas). The provider should document (in its module docstring) which subset botcircuits explicitly relies on.
- **Cost/rate limits**: OpenRouter has its own per-key rate limits and credit-based billing, separate from vendor-direct billing — worth a one-line callout in `DEVELOPMENT.md` for anyone enabling it.
- **`cli/app.py` duplication**: `make_provider()` is duplicated between `providers/__init__.py` and `cli/app.py` (`src/botcircuits/cli/app.py:192`) — both need the new branch, or this drifts again next time a provider is added.

## 6. Revision History
- 2026-07-02: Initial plan drafted directly in conversation (not via the `agentic-loop` workflow — that run was blocked by a `claude-code` runtime auth conflict in this environment: ANTHROPIC_API_KEY auth taking precedence over claude.ai login, causing every segment to fail). Treat this as a first-pass plan a human should sanity-check before implementation begins.
- 2026-07-02: Implemented. `src/botcircuits/providers/openrouter.py` (new `OpenRouterProvider(OpenAIProvider)`, pinned to `https://openrouter.ai/api/v1` + attribution headers); registered in `providers/__init__.py::make_provider` and `cli/app.py::make_provider` (both needed the API key threaded explicitly — the OpenAI SDK only auto-infers `OPENAI_API_KEY`, not `OPENROUTER_API_KEY`, so relying on implicit inference silently breaks auth); added to `cli/setup.py::PROVIDER_CATALOG`, `workflow_validator.py::_VALID_PROVIDERS`, and `StepPanel.tsx::FALLBACK_PROVIDERS`; updated `.env.example` and `DEVELOPMENT.md`. Added `tests/test_provider_openrouter.py` plus extensions to `test_workflow_validator.py` and `test_manager_backend.py` (the latter had a hardcoded provider-set assertion that needed updating). Full suite: 295/295 passing; `tsc --noEmit` clean.
  - **Not yet verified**: the live round-trip against a real `OPENROUTER_API_KEY` (none was available in this environment) — specifically whether the Responses API beta handles tool-calling correctly for non-OpenAI-hosted models. Run the verification spike from §2 before relying on this in production.

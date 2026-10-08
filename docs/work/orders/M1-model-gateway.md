# M1 — Model-gateway MCP with a capability routing table

ADR: [AT-0004](../../adr/AT-0004-portable-kanban-suite.md) (Phase 2; the Principal: the routing table through MCP "is exactly what I was gesturing at"). Header: [ARM-HEADER](ARM-HEADER.md). Dispatch: FEATURE + TEST.

## Problem

Bound agents need a model-agnostic way to hand modality work to dedicated models. The Cline bot (the sidebar assistant) needs this first. Examples are image generation, audio transcription and speech, and other specialised capabilities. The work must go through a governed path, not as new agent sessions.

What exists today:
- `_impl/service/openrouter_models.py`: a bounded, credential-safe OpenRouter model metadata registry.
- The summarizer's budgeted, diagnostics-safe OpenRouter request code (`_impl/service/episodic_summarizer.py`), which is today's only outbound model call.

## Interface surface

- **Server.** Console script `kp-agent-models` (core package), run as `kp-agent-models --config <path> serve`, a stdio MCP server. Its config schema is `agent-tooling.model-gateway.v1`:
  - `routes`: capability → `{provider, model, params?}`;
  - `providers`: `{kind: "openrouter"|"openai-compatible"|…, base_url, api_key_file}`, where each key file is private (0600) and never logged;
  - `budgets`: per-capability `{max_calls_per_hour, max_usd_per_day, confirm_over_usd?}`;
  - `artifact_root`.
- **Tools.** One tool per configured capability, named `model.<capability>`. The initial capability set includes `image.generate`, `audio.transcribe`, `audio.speak` and `text.complete`. Unconfigured capabilities are not listed.
  - Inputs are bounded.
  - Binary inputs are artifact references under `artifact_root`, not inline blobs larger than a bound.
  - Outputs are artifact references with provenance: capability, provider, model, request digest, cost if reported, and timestamp.
- **`kp-agent-models --config … doctor`** reports each route's status and the budget state. It never makes a paid call unless `--live` is given.
- **Budgets.** A call that would exceed a cap is refused before any network request, with a structured reason. With `confirm_over_usd` set, a call estimated above that amount returns `confirmation_required` and makes no call; the tool accepts an explicit `confirm: true` on retry.
- **Scoping.** The gateway is not tied to a desk. When launched through a binding (T3), its artifacts and provenance events are recorded against the bound session's desk through the existing memory store, if the launch config provides one.

## Properties and falsifiers

- **P1: routing is configuration.** Changing a capability's `provider` or `model` in the config changes the outbound request's target with no code change. Adding a capability to `routes`, with a provider kind that already exists, lists a new tool.
  Falsifier: a code change needed, or a stale route used.
- **P2: secrets are safe.** API keys are read only from `api_key_file` (private, owner-only). They never appear in tool output, errors, logs, provenance or artifacts. A world-readable key file is refused.
  Falsifier: a key in any output, or a permissive key file accepted.
- **P3: budgets are enforced before the network.** A capped or unconfirmed call makes no request. Counters survive a restart.
  Falsifier: a request sent past a cap, or counters reset on restart.
- **P4: artifacts have provenance.** Every successful call writes its output under `artifact_root` with a provenance record, and the tool returns only the reference and metadata. Paths never escape the root.
  Falsifier: missing provenance, inline oversized output, or path traversal.
- **P5: failures are honest.** A provider error or an unsupported modality returns a structured error naming the capability and provider class. Nothing is written as success, and no paid retry happens silently.
  Falsifier: a success-shaped result on failure, or a hidden retry.

## Write scope

- **FEATURE:** `packages/tooling/src/kp_agent_tooling/models_cli.py`, a new `_impl/service/model_gateway.py` (and helpers), the `[project.scripts]` entry, reuse of `openrouter_models.py` and the summarizer's request code (refactor them into a shared client only if needed, keeping every existing test unmodified), and a new `docs/MODEL-GATEWAY.md`.
  - Verify current OpenRouter modality support from primary documentation, and cite it in the doc and report. Where OpenRouter lacks a modality, the `openai-compatible` provider kind covers direct providers.
  - Do not make live paid calls. There is no live key in scope; use a local stub HTTP server.
- **TEST:** new tests under `tests/models/`, using a local stub OpenAI/OpenRouter-compatible HTTP server under `TMPDIR` and the stdio MCP server. No network.

# Model gateway (`kp-agent-models`)

The model gateway is a stdio MCP server that hands modality work (images, audio,
text) to dedicated models. A routing table in the configuration maps each
capability to a provider and a model. The gateway is not tied to a desk. It is
not an agent session and it dispatches nothing. Order:
[M1](work/orders/M1-model-gateway.md). ADR: [AT-0004](adr/AT-0004-portable-kanban-suite.md),
Phase 2.

```
kp-agent-models --config <path> [--memory-config <path>] serve
kp-agent-models --config <path> doctor [--live]
kp-agent-models --config <path> tools
kp-agent-models --config <path> call --tool model.<capability> --arguments '<json>'
```

The configuration is re-read for every tool listing and every call. A route
change takes effect on the next call with no restart and no code change.
MCP clients cache tool lists, so they see a new capability after they list
tools again.

## Configuration: `agent-tooling.model-gateway.v1`

```json
{
  "schema_version": "agent-tooling.model-gateway.v1",
  "artifact_root": "/abs/path/model-artifacts",
  "providers": {
    "openrouter": {"kind": "openrouter", "base_url": "https://openrouter.ai/api/v1",
                   "api_key_file": "/abs/path/openrouter.key"},
    "direct": {"kind": "openai-compatible", "base_url": "https://api.openai.com/v1",
               "api_key_file": "/abs/path/openai.key"}
  },
  "routes": {
    "image.generate":   {"provider": "openrouter", "model": "google/gemini-2.5-flash-image",
                         "params": {"aspect_ratio": "1:1"}},
    "audio.transcribe": {"provider": "openrouter", "model": "openai/whisper-1"},
    "audio.speak":      {"provider": "openrouter", "model": "openai/gpt-4o-mini-tts-2025-12-15",
                         "params": {"voice": "alloy", "response_format": "mp3"}},
    "text.complete":    {"provider": "direct", "model": "gpt-4o-mini"}
  },
  "budgets": {
    "image.generate":   {"max_calls_per_hour": 20, "max_usd_per_day": 2.0, "confirm_over_usd": 0.10},
    "audio.transcribe": {"max_calls_per_hour": 30, "max_usd_per_day": 1.0},
    "audio.speak":      {"max_calls_per_hour": 30, "max_usd_per_day": 1.0},
    "text.complete":    {"max_calls_per_hour": 60, "max_usd_per_day": 1.0}
  }
}
```

The model ids above are examples. Check the current model ids at the provider.

### Top level

| Field | Required | Meaning |
|---|---|---|
| `schema_version` | yes | Always `agent-tooling.model-gateway.v1`. |
| `artifact_root` | yes | An existing absolute directory that is not a symlink. No fallback is ever created. |
| `providers` | yes | Provider name mapped to a provider entry. At most 64. |
| `routes` | yes | Capability mapped to a route. At most 64. |
| `budgets` | no | Capability mapped to a budget. A routed capability without a budget is listed, but every call to it is refused with `budget_unconfigured`. |
| `memory_config` | no | Absolute path of the bound session's desk-memory configuration (see Scoping). |

Unknown keys are refused at every level.

### Providers

| Field | Required | Meaning |
|---|---|---|
| `kind` | yes | `openrouter` or `openai-compatible`. |
| `base_url` | yes | Use `https`. Plain `http` is accepted only for loopback hosts, unless `allow_plain_http: true` is set. No credentials, query or fragment. |
| `api_key_file` | yes | Absolute path. The file must be a regular file (not a symlink), owned by the running user, with no group or other permission bits (`0600` or `0400`). It must hold one key token. |
| `timeout_seconds` | no | 1 to 600. Default 120. This is the total time allowed for the one request. |
| `capabilities` | no | Narrows which capabilities this provider may serve. A route outside this list is an unsupported modality. |
| `allow_plain_http` | no | Explicit opt-in to plain HTTP for a non-loopback host, for example a provider on a private container network. |

The key file is checked and read at call time, only when a request is about
to be sent. Its contents are used only for the `Authorization` header. A
permissive file is refused before it is read, with `secret_refused` and
`key_file_status: "permissions_too_open"`. Other refusal reasons are `missing`,
`symlink`, `not_regular_file`, `not_owner`, `empty_or_oversized` and
`invalid_content`. The key never appears in any of these:
- tool results or errors;
- stdout or stderr;
- provenance or artifacts;
- the ledger or memory.

A provider response that echoes the key is discarded as
`provider_response_contains_secret`.

### Routes

| Field | Required | Meaning |
|---|---|---|
| `provider` | yes | The name of a configured provider. |
| `model` | yes | The provider's model id. |
| `params` | no | Extra request fields, passed through as given (8 KiB at most). Fields the gateway owns are refused: `model`, `prompt`, `input`, `messages`, `input_audio`, `file`, `stream` and `stream_options`. |
| `operation` | no | The wire operation for a capability outside the initial set: `image`, `transcription`, `speech` or `chat`. |

Capability names look like `family.verb` (for example `image.generate`). Each
routed capability is listed as the tool `model.<capability>`. Unrouted
capabilities are not listed and cannot be called.

### Budgets

| Field | Required | Meaning |
|---|---|---|
| `max_calls_per_hour` | yes | Calls counted over a rolling 3600 seconds. |
| `max_usd_per_day` | yes | Spend counted over a rolling 86400 seconds. |
| `confirm_over_usd` | no | A call whose estimate is above this amount, or unknown, returns `confirmation_required`. |
| `estimated_usd_per_call` | no | The operator's per-call estimate. |

The estimate comes from `estimated_usd_per_call` when that is set. Otherwise it
is the highest provider-reported cost for the same capability, provider and
model in the last 30 days. Otherwise it is unknown.

## In the Docker runtime

The gateway runs in the `tooling` role of the Docker runtime
([DOCKER.md](DOCKER.md#first-run-from-an-empty-root), step 7), over
`docker exec -i` like the other MCP servers. Its files live in the runtime root:

| Runtime root path | Container path | Notes |
|---|---|---|
| `$root/config/models/gateway.json` | `/config/models/gateway.json` | The configuration, 0600. |
| `$root/config/models/openrouter.key` | `/config/models/openrouter.key` | The key file, 0600, one key token. `/config` is mounted read-only into every tooling role (`tooling`, `refresh`, `capture`, `board`), so each of them can read it. |
| `$root/state/model-artifacts/` | `/state/model-artifacts` | `artifact_root`. **Create it before the first call** (`mkdir -m 700`): the gateway refuses a missing `artifact_root` and never creates one. |

```sh
mkdir -m 700 "$root/config/models" "$root/state/model-artifacts"
printf '%s\n' "$OPENROUTER_API_KEY" > "$root/config/models/openrouter.key"   # with umask 077
docker exec -i "${project}-tooling" kp-agent-models --config /config/models/gateway.json doctor
```

The configuration names the container paths: `"artifact_root": "/state/model-artifacts"`
and `"api_key_file": "/config/models/openrouter.key"`. Write the files as the user
the containers run as (`kp-agent-install --uid`, by default the user who planned):
the key file check requires that owner. Register the server as
`{"command": "docker", "args": ["exec", "-i", "<project>-tooling", "kp-agent-models", "--config", "/config/models/gateway.json", "serve"]}`.

## Capabilities and wire shapes

| Capability (default) | Operation | `openrouter` | `openai-compatible` |
|---|---|---|---|
| `image.generate` | image | `POST {base}/images` (JSON `{model, prompt, …params}`), response `data[].b64_json` and `media_type` | `POST {base}/images/generations`, response `data[].b64_json` |
| `audio.transcribe` | transcription | `POST {base}/audio/transcriptions` (JSON `input_audio: {data, format}`) | `POST {base}/audio/transcriptions` (multipart: `file`, `model`, …) |
| `audio.speak` | speech | `POST {base}/audio/speech` (JSON `{model, input, voice, response_format}`), response is raw audio bytes | same |
| `text.complete` | chat | `POST {base}/chat/completions` | same |

An unsupported modality returns `unsupported_modality` before any request,
naming the capability, the provider and the provider class. That happens in
three cases:
- a capability outside this table has no `operation`;
- a provider's `capabilities` list excludes the capability;
- the provider class lacks the operation.

The tool is still listed, so an agent can see that it is configured.

Image results that carry only a `url` (no `b64_json`) are refused as
`provider_response_unsupported`. Fetching them would need a second,
unbudgeted request to an arbitrary host.

### Tool inputs (bounded)

| Operation | Inputs |
|---|---|
| image | `prompt` (1 to 4000 characters) |
| speech | `text` (1 to 4096 characters), optional `voice` (`[A-Za-z0-9_.-]{1,64}`) |
| transcription | `format` (`wav`, `mp3`, `flac`, `m4a`, `ogg`, `webm` or `aac`), plus exactly one of `audio_ref` or `audio_base64`; optional `language` (ISO 639-1) |
| chat | `prompt` (1 to 32000 characters), optional `system` (up to 8000 characters), optional `max_tokens` (1 to 16384) |

Every tool also accepts `confirm: true`, and refuses extra fields.

Binary input is an artifact reference, `audio_ref`: a path under
`artifact_root`, either relative or absolute inside it. The input artifact must
meet these rules:
- It must resolve inside the root. `..`, symlinks that escape the root, and the
  gateway's own `.model-gateway/` state are refused (`input_unavailable`).
- It must be a non-empty regular file of at most 25 MiB.

Inline `audio_base64` is accepted only up to 256 KiB decoded.

### Outputs, artifacts and provenance

A successful call writes its outputs and a `provenance.json` into a staging
directory, then publishes them with one rename to this path:

```
<artifact_root>/outputs/<capability>/<UTC date>/<call_id>/output-<n>.<ext>
<artifact_root>/outputs/<capability>/<UTC date>/<call_id>/provenance.json
```

Files are mode `0600`. The tool result carries only references and metadata,
never the output bytes. The result contains:
- `artifacts[]`: each with `ref`, `path`, `media_type`, `bytes` and `sha256`;
- `provenance_ref`;
- the `provenance` record.

Provenance (`agent-tooling.model-gateway.provenance.v1`) records:
- the capability, tool, operation, provider, provider class and model;
- `request_target`: scheme, host and path only;
- `request_digest`: sha256 of the exact request body, plus `request_bytes`;
- `started_at` and `timestamp`;
- `cost_usd` when the provider reported a cost (`cost_reported` says whether it did);
- `usage`, limited to numeric fields;
- `generation_id`, `model_reported` and `finish_reason`;
- `confirmed`, `estimate_usd` and `estimate_source`;
- `inputs`: each input's sha256, byte count and reference.

Prompts and outputs are not copied into provenance.

## Budgets: enforced before the network

The ledger is `<artifact_root>/.model-gateway/ledger.sqlite3` (mode `0600`).
Counters persist across restarts. A call is checked and reserved in one
`BEGIN IMMEDIATE` transaction, so concurrent calls cannot both pass a cap. In
order:

1. If the calls in the last hour plus this one would exceed `max_calls_per_hour`,
   the call is refused: `budget_exceeded`, `reason: max_calls_per_hour`.
2. If the spend in the last 24 hours has reached `max_usd_per_day`, or that
   spend plus a known estimate would exceed it, the call is refused:
   `budget_exceeded`, `reason: max_usd_per_day`.
3. If `confirm_over_usd` is set, `confirm` is not `true`, and the estimate is
   above the threshold or unknown, the result is `status: confirmation_required`
   with a reason of `estimate_over_threshold` or `estimate_unknown`. No request
   is sent. Retry the same call with `confirm: true`.

Every refusal and every `confirmation_required` result says `requests_sent: 0`.
Spend counts the provider-reported cost, or else the reservation's estimate.
Attempts that reached the provider count toward both caps, even when they
failed. An attempt whose connection was refused is recorded as `not_sent` and
does not count.

## Failures are honest

Each call sends at most one request. There are no retries and no redirects. A
provider failure returns these fields:
- `status: error` and `category: provider_error`;
- the capability, provider and `provider_class`;
- `failure`, one of `provider_http_error`, `provider_timeout`,
  `provider_transport_error`, `provider_response_invalid`,
  `provider_response_oversized`, `provider_response_unsupported` or
  `provider_response_contains_secret`;
- `http_status`, when there is one;
- `provider_error`, reduced to the summarizer's content-free vocabulary
  (`safe_provider_error`);
- `requests_sent` (1, or 0 when the connection was refused), `billing`, and
  `retry: none`.

Nothing is written as success. A failed write leaves no published output
directory. MCP results set `isError` for every status except `ok`.

## Doctor

`doctor` prints `agent-tooling.model-gateway.doctor.v1`. For each route it
reports:
- the provider, the provider class and the model;
- the operation and `request_target`;
- modality support and key-file status;
- the budget state: calls in the last hour, USD in the last 24 hours, the
  estimate and its source;
- a route status: `ready`, `unsupported_modality`, `budget_unconfigured` or
  `secret_refused`.

It exits 0 only when every route is ready. Without `--live` it sends no
requests. With `--live` it sends only unpaid model-metadata `GET {base}/models`
requests: OpenRouter's request adds `?output_modalities=text|image|speech|transcription`.
It then reports whether each routed model is listed. For OpenRouter it also
reports per-token pricing, parsed by the existing bounded registry parser in
`openrouter_models.py`. The doctor never makes a paid call, with or without
`--live`.

## Scoping: desk memory when launched through a binding

The gateway works without a desk. When a launch (T3) provides the bound
session's memory configuration, through `--memory-config` or `memory_config`,
each successful call is also recorded as a provenance event against that
session's desk. The memory configuration is `ops.desk-memory.local.v1`, or
`ops.assistant-memory.local.v1` for the Cline bot's isolated memory.

The event goes through the existing episode store: `components()`, then
`EpisodeStore.capture()`. It is one `tool` event holding the provenance JSON,
with `source_ref` `model-gateway:<call_id>`. The seal appends a row to the
store's index outbox, and the gateway never writes the search index itself
(T12b, [DOCKER.md](DOCKER.md#the-indexer)). On a host install
(`AGENT_MEMORY_VOLUME` unset) it drains the outbox once after the seal,
without waiting for the index lease. The result's `memory_record` reports one
of these:
- `recorded`, with `episode_id`, `binding_key` and `indexed`. `indexed` is
  true only when this call's own drain ran to completion, so the outbox, this
  seal included, was applied and the new episode is in the index. It is false
  when this call did not complete a drain: another drainer held the lease, the drain failed (the row waits for
  the next drain), or the gateway runs inside the Docker runtime, where it is
  always false because only the `indexer` role drains;
- `skipped`, when the desk registry disables capture;
- `unavailable`, when the session is not admitted;
- `not_configured`.

A memory failure never hides a completed, possibly billed, call. Neither the
model nor tool arguments can choose a desk.

## Primary sources (retrieved 2026-09-30)

These OpenRouter documentation pages were read on 2026-09-30:
- **Image generation** uses the dedicated `POST /api/v1/images`. The request
  takes `model`, `prompt` and optional `resolution`, `aspect_ratio`, `quality`,
  `output_format`, `n` and `provider` fields. The response is `data[].b64_json`
  with `media_type` and `usage.cost`. Billing is all-or-nothing.
  - <https://openrouter.ai/docs/guides/overview/multimodal/image-generation>
  - <https://openrouter.ai/docs/features/multimodal/image-generation> (same content)
- **Speech-to-text** is `POST /api/v1/audio/transcriptions`. The JSON request
  is `input_audio: {data (raw base64), format}`, and OpenAI-style multipart is
  also accepted. The response is `{text, usage{seconds, cost, …}}`. Multipart
  uploads are limited to 25 MB. Models are discovered with
  `/models?output_modalities=transcription`.
  - <https://openrouter.ai/docs/guides/overview/multimodal/stt>
- **Text-to-speech** is `POST /api/v1/audio/speech`, with `model`, `input`,
  `voice`, `response_format` (`mp3` or `pcm`, default `pcm`) and `speed`. The
  response is a raw audio byte stream (`audio/mpeg` or `audio/pcm`) with an
  `X-Generation-Id` header. Pricing is per character.
  - <https://openrouter.ai/docs/guides/overview/multimodal/tts>
  - <https://openrouter.ai/docs/api/api-reference/tts/create-speech>
- **Audio input and output in chat completions:** chat completions accepts
  `input_audio` content (base64 only). Audio output in chat needs
  `modalities: ["text","audio"]` and arrives only as a stream. The gateway uses
  the dedicated speech endpoint instead.
  - <https://openrouter.ai/docs/features/multimodal/audio>
- **Chat completions API reference:** `modalities` takes `text`, `image` and
  `audio`. The reference also has a provider-specific `image_config`, and
  `message.images` / `message.audio` on responses.
  - <https://openrouter.ai/docs/api/api-reference/chat/send-chat-completion-request>
  - <https://openrouter.ai/docs/api/api-reference/chat/create-a-chat-completion>
- **Multimodal overview:** this page lists image generation under
  `/api/v1/chat/completions`, unlike the image-generation guide above. The
  gateway follows the dedicated guide (`/api/v1/images`). The chat route stays
  available as the documented alternative, but the gateway does not implement
  it.
  - <https://openrouter.ai/docs/guides/overview/multimodal/overview>

Not retrieved: <https://openrouter.ai/docs/api/api-reference/images/create-images>
returned 404 to the fetcher.

For `openai-compatible`, multipart `POST /v1/audio/transcriptions` was confirmed
from the OpenAI API reference listing
(<https://developers.openai.com/api/reference/resources/audio/subresources/transcriptions/methods/create>),
read through search on 2026-09-30. The `/images/generations` `b64_json` shape was
not re-read from primary documentation in this session (the platform pages
refused the fetcher). The provider `capabilities` declaration and the route
`operation` field let an operator declare what a direct provider supports
without a code change.

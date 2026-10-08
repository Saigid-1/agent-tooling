"""Auditable model profile and lossless source slices for episodic consolidation.

The OpenRouter adapter consumes a caller supplied models response. It makes no
request, and endpoint selection remains the caller's responsibility.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

from kp_agent_tooling._impl import leaf


# Accepted baseline; explicit profiles can select another model.
DEFAULT_SUMMARY_MODEL = "z-ai/glm-5.3-flash"


def _positive(value: object, name: str) -> int:
    if type(value) is not int or value <= 0 or value > 100_000_000:
        raise ValueError(f"{name} must be a positive bounded integer")
    return value


def _nonnegative(value: object, name: str) -> int:
    if type(value) is not int or value < 0 or value > 100_000_000:
        raise ValueError(f"{name} must be a non-negative bounded integer")
    return value


def _digest(value: object) -> str:
    return leaf.canonical_sha256(value, ascii=True, allow_nan=True)


@dataclass(frozen=True, slots=True)
class MemoryProfile:
    model_id: str
    provider_id: str
    source: str
    observed_at: datetime
    metadata_sha256: str
    context_tokens: int
    max_output_tokens: int | None
    tool_calling: bool | None
    retention: str  # endpoint-specific; unknown unless a selected route is supplied
    native_compaction: str  # harness capability, not a model property


def assert_profile_current(profile: MemoryProfile, *, now: datetime | None = None,
                           max_age: timedelta = timedelta(hours=24)) -> None:
    if not isinstance(profile, MemoryProfile):
        raise ValueError("validated profile required")
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None or not timedelta(0) <= now - profile.observed_at <= max_age:
        raise ValueError("model metadata is stale or from the future")


def openrouter_profile(
    entry: Mapping[str, object], *, provider_id: str, observed_at: datetime,
    route: Mapping[str, object] | None = None,
    max_age: timedelta = timedelta(hours=24), now: datetime | None = None,
) -> MemoryProfile:
    """Validate one /api/v1/models entry and optional selected endpoint facts.

    ``route`` may contain context_length, max_completion_tokens and retention.
    Its identity must match provider_id. Unknown route retention remains unknown.
    """
    if not isinstance(entry, Mapping) or not isinstance(provider_id, str) or not provider_id:
        raise ValueError("model entry and provider id required")
    if not isinstance(observed_at, datetime) or observed_at.tzinfo is None:
        raise ValueError("timezone-aware metadata timestamp required")
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None or not timedelta(0) <= now - observed_at <= max_age:
        raise ValueError("model metadata is stale or from the future")
    model_id = entry.get("id")
    if not isinstance(model_id, str) or not 1 <= len(model_id) <= 256 or model_id != model_id.strip():
        raise ValueError("invalid model id")
    context = _positive(entry.get("context_length"), "context_length")
    top = entry.get("top_provider")
    if top is not None and not isinstance(top, Mapping):
        raise ValueError("invalid top_provider")
    top = top or {}
    if top.get("context_length") is not None:
        context = min(context, _positive(top["context_length"], "top_provider.context_length"))
    output = top.get("max_completion_tokens")
    if output is not None:
        output = _positive(output, "top_provider.max_completion_tokens")
    retention = "unknown"
    if route is not None:
        if not isinstance(route, Mapping) or route.get("provider_id") != provider_id:
            raise ValueError("selected route provider identity mismatch")
        if route.get("context_length") is not None:
            context = min(context, _positive(route["context_length"], "route.context_length"))
        if route.get("max_completion_tokens") is not None:
            route_output = _positive(route["max_completion_tokens"], "route.max_completion_tokens")
            output = min(output, route_output) if output is not None else route_output
        retention = route.get("retention", "unknown")
        if retention not in {"unknown", "zero", "nonzero"}:
            raise ValueError("invalid route retention")
    parameters = entry.get("supported_parameters")
    if parameters is not None and (not isinstance(parameters, list) or
                                   any(not isinstance(p, str) for p in parameters)):
        raise ValueError("invalid supported_parameters")
    tools = None if parameters is None else "tools" in parameters
    receipt = {"provider_id": provider_id, "model": dict(entry),
               "route": dict(route) if route is not None else None}
    return MemoryProfile(model_id, provider_id, "openrouter:/api/v1/models", observed_at,
                         _digest(receipt), context, output, tools, retention, "unknown")


@dataclass(frozen=True, slots=True)
class MemoryBudget:
    profile: MemoryProfile
    system_tokens: int
    tool_tokens: int
    reasoning_tokens: int
    output_tokens: int
    safety_tokens: int
    input_tokens: int


def memory_budget(profile: MemoryProfile, *, system_tokens: int, tool_tokens: int,
                  reasoning_tokens: int, output_tokens: int, safety_tokens: int,
                  now: datetime | None = None) -> MemoryBudget:
    """Reserve all non-source context before allocating a source window."""
    assert_profile_current(profile, now=now)
    reserves = [_nonnegative(v, n) for n, v in (
        ("system_tokens", system_tokens), ("tool_tokens", tool_tokens),
        ("reasoning_tokens", reasoning_tokens), ("safety_tokens", safety_tokens))]
    requested_output = _positive(output_tokens, "output_tokens")
    if profile.max_output_tokens is not None and requested_output > profile.max_output_tokens:
        raise ValueError("output reserve exceeds route maximum")
    available = profile.context_tokens - sum(reserves) - requested_output
    if available <= 0:
        raise ValueError("no source context remains after reserves")
    return MemoryBudget(profile, *reserves[:3], requested_output, reserves[3], available)


def load_memory_budget_receipt(receipt: Mapping[str, object], *,
                               now: datetime | None = None) -> MemoryBudget:
    """Revalidate raw metadata, its hash, timestamp and all budget arithmetic."""
    if not isinstance(receipt, Mapping) or receipt.get("schema_version") != "ops.memory-model-profile.v1":
        raise ValueError("invalid memory profile receipt")
    saved_profile, saved_budget = receipt.get("profile"), receipt.get("budget")
    if not isinstance(saved_profile, Mapping) or not isinstance(saved_budget, Mapping):
        raise ValueError("memory profile receipt lacks validated fields")
    try:
        observed = datetime.fromisoformat(saved_profile["observed_at"])
        profile = openrouter_profile(receipt["raw_model_entry"],
                                     provider_id=saved_profile["provider_id"],
                                     observed_at=observed,
                                     route=receipt.get("selected_route"), now=now)
        budget = memory_budget(profile,
                               system_tokens=saved_budget["system_tokens"],
                               tool_tokens=saved_budget["tool_tokens"],
                               reasoning_tokens=saved_budget["reasoning_tokens"],
                               output_tokens=saved_budget["output_tokens"],
                               safety_tokens=saved_budget["safety_tokens"], now=now)
    except (KeyError, TypeError, AttributeError) as error:
        raise ValueError("invalid memory profile receipt") from error
    expected_profile = asdict(profile)
    expected_profile["observed_at"] = observed.isoformat()
    if dict(saved_profile) != expected_profile:
        raise ValueError("memory profile receipt metadata hash or facts mismatch")
    expected_budget = {field: getattr(budget, field) for field in (
        "system_tokens", "tool_tokens", "reasoning_tokens", "output_tokens",
        "safety_tokens", "input_tokens")}
    if dict(saved_budget) != expected_budget:
        raise ValueError("memory profile receipt budget mismatch")
    return budget


@dataclass(frozen=True, slots=True)
class SourceSlice:
    episode_id: str
    event_id: str
    role: str
    start: int
    end: int
    text: str
    estimated_tokens: int
    counter: str


def utf8_byte_estimate(text: str) -> int:
    """Conservative planning estimate: one token per UTF-8 byte; not a tokenizer proof."""
    return len(text.encode("utf-8"))


def chunk_events(events: Sequence[Mapping[str, str]], *, episode_id: str,
                 budget: MemoryBudget, count_tokens: Callable[[str], int] = utf8_byte_estimate,
                 tokenizer_name: str = "utf8-byte-estimate",
                 max_slices: int = 4096) -> tuple[SourceSlice, ...]:
    """Split visible events at character coordinates, preserving every character.

    The caller must reserve serialization/prompt framing in ``system_tokens``.
    Every returned span is independently bounded by the source token budget.
    """
    if not isinstance(episode_id, str) or not episode_id or len(episode_id) > 256:
        raise ValueError("bounded episode identity required")
    if not isinstance(budget, MemoryBudget) or not callable(count_tokens) or not tokenizer_name:
        raise ValueError("budget and named token counter required")
    if type(max_slices) is not int or not 1 <= max_slices <= 4096:
        raise ValueError("max_slices must be 1..4096")
    if not isinstance(events, Sequence) or isinstance(events, (str, bytes)) or len(events) > 500:
        raise ValueError("bounded event sequence required")
    slices: list[SourceSlice] = []
    for event in events:
        if not isinstance(event, Mapping) or set(event) != {"event_id", "role", "text"}:
            raise ValueError("event_id, role, text required")
        event_id, role, value = event["event_id"], event["role"], event["text"]
        if not isinstance(event_id, str) or not event_id or len(event_id) > 128 or role not in {"user", "assistant", "tool"} or not isinstance(value, str) or not value:
            raise ValueError("invalid visible event")
        if len(value.encode("utf-8")) > 128000:
            raise ValueError("event exceeds EpisodeStore capture bound")
        start = 0
        while start < len(value):
            low, high = start + 1, len(value)
            best = start
            while low <= high:
                middle = (low + high) // 2
                cost = count_tokens(value[start:middle])
                if type(cost) is not int or cost < 0:
                    raise ValueError("token counter returned invalid count")
                if cost <= budget.input_tokens:
                    best, low = middle, middle + 1
                else:
                    high = middle - 1
            if best == start:
                raise ValueError("one character exceeds source budget")
            part = value[start:best]
            slices.append(SourceSlice(episode_id, event_id, role, start, best,
                                      part, count_tokens(part), tokenizer_name))
            if len(slices) > max_slices:
                raise ValueError("source requires more than max_slices")
            start = best
    return tuple(slices)

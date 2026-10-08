"""Bounded, credential-safe OpenRouter model metadata registry."""

from __future__ import annotations

import json
import math
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from http.client import HTTPSConnection
from threading import Lock
from types import MappingProxyType
from typing import Any


OPENROUTER_MODELS_HOST = "openrouter.ai"
OPENROUTER_MODELS_PATH = "/api/v1/models"
DEFAULT_MODELS_TIMEOUT_SECONDS = 5.0
DEFAULT_MODELS_CACHE_SECONDS = 60.0

MAX_MODEL_COUNT = 10_000
MAX_MODELS_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_MODEL_ID_LENGTH = 256
MAX_PRICE_LENGTH = 64
MAX_CONTEXT_LENGTH = 100_000_000

_OPENROUTER_CREDENTIAL = re.compile(r"^sk-or-v1-[A-Za-z0-9_-]{12,}$")


class OpenRouterModelRegistryError(ValueError):
    """The OpenRouter model registry could not be fetched or parsed safely."""


@dataclass(frozen=True, slots=True)
class ModelInfo:
    """Bounded public metadata needed to decide whether a model fits a dispatch.

    OpenRouter prices are represented as dollars per token, matching the models
    endpoint's pricing strings without rounding or floating-point conversion.
    """

    model_id: str
    context_length: int
    prompt_price: Decimal
    completion_price: Decimal


def _openrouter_models_json(api_key: str, timeout: float) -> Any:
    """Fetch the pinned models endpoint without exposing credentials in errors."""

    connection = HTTPSConnection(OPENROUTER_MODELS_HOST, timeout=timeout)
    try:
        connection.request(
            "GET",
            OPENROUTER_MODELS_PATH,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
            },
        )
        response = connection.getresponse()
        body = response.read(MAX_MODELS_RESPONSE_BYTES + 1)
        if response.status != 200:
            raise OpenRouterModelRegistryError(
                "OpenRouter models request was not successful"
            )
        if len(body) > MAX_MODELS_RESPONSE_BYTES:
            raise OpenRouterModelRegistryError(
                "OpenRouter models response exceeded the size limit"
            )
        return json.loads(body)
    except Exception:
        raise OpenRouterModelRegistryError("OpenRouter models request failed") from None
    finally:
        connection.close()


def _price_decimal(value: object) -> Decimal:
    if not isinstance(value, str) or not value or len(value) > MAX_PRICE_LENGTH:
        raise ValueError("model price is not a bounded decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        raise ValueError("model price is not a decimal string") from None
    if not parsed.is_finite() or parsed < 0:
        raise ValueError("model price must be finite and non-negative")
    return parsed


def _parse_model(entry: object, api_key: str) -> ModelInfo:
    if not isinstance(entry, Mapping):
        raise ValueError("model entry must be an object")

    model_id = entry.get("id")
    if (
        not isinstance(model_id, str)
        or not model_id
        or len(model_id) > MAX_MODEL_ID_LENGTH
        or model_id != model_id.strip()
        or any(ord(character) < 32 for character in model_id)
        or model_id == api_key
        or _OPENROUTER_CREDENTIAL.fullmatch(model_id) is not None
    ):
        raise ValueError("model id is invalid or credential-shaped")

    context_length = entry.get("context_length")
    if (
        isinstance(context_length, bool)
        or not isinstance(context_length, int)
        or context_length <= 0
        or context_length > MAX_CONTEXT_LENGTH
    ):
        raise ValueError("model context_length is invalid")

    pricing = entry.get("pricing")
    if not isinstance(pricing, Mapping):
        raise ValueError("model pricing must be an object")

    return ModelInfo(
        model_id=model_id,
        context_length=context_length,
        prompt_price=_price_decimal(pricing.get("prompt")),
        completion_price=_price_decimal(pricing.get("completion")),
    )


def _price_ceiling(value: Decimal | None, field: str) -> Decimal | None:
    if value is None:
        return None
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise ValueError(f"{field} must be a finite non-negative Decimal")
    return value


def fitting_models(
    registry: Mapping[str, ModelInfo],
    *,
    min_context_window: int,
    max_prompt_price: Decimal | None = None,
    max_completion_price: Decimal | None = None,
) -> tuple[ModelInfo, ...]:
    """Return models satisfying dispatch limits, largest context first."""

    if (
        isinstance(min_context_window, bool)
        or not isinstance(min_context_window, int)
        or min_context_window < 0
    ):
        raise ValueError("min_context_window must be a non-negative integer")
    prompt_ceiling = _price_ceiling(max_prompt_price, "max_prompt_price")
    completion_ceiling = _price_ceiling(
        max_completion_price, "max_completion_price"
    )
    candidates = (
        model
        for model in registry.values()
        if model.context_length >= min_context_window
        and (prompt_ceiling is None or model.prompt_price <= prompt_ceiling)
        and (
            completion_ceiling is None
            or model.completion_price <= completion_ceiling
        )
    )
    return tuple(
        sorted(candidates, key=lambda model: (-model.context_length, model.model_id))
    )


class OpenRouterModelRegistryClient:
    """Fetch and briefly cache OpenRouter's public per-model metadata.

    The outer response must be ``{"data": list}`` and the list is capped.
    Malformed individual entries are skipped. Only validated bounded fields are
    copied, and model identifiers that equal or resemble credentials are skipped.
    Fetch and parse failures use fixed messages that contain no payload or key.
    """

    def __init__(
        self,
        api_key: str,
        *,
        timeout: float = DEFAULT_MODELS_TIMEOUT_SECONDS,
        cache_seconds: float = DEFAULT_MODELS_CACHE_SECONDS,
        fetch_json: Callable[[str, float], Any] = _openrouter_models_json,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(api_key, str) or not api_key:
            raise ValueError("OpenRouter API key must be a non-empty string")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
            or isinstance(cache_seconds, bool)
            or not isinstance(cache_seconds, (int, float))
            or not math.isfinite(cache_seconds)
            or cache_seconds < 0
        ):
            raise ValueError("models timeout must be positive and cache non-negative")
        self._api_key = api_key
        self._timeout = float(timeout)
        self._cache_seconds = float(cache_seconds)
        self._fetch_json = fetch_json
        self._clock = clock
        self._cached_at: float | None = None
        self._cached_registry: Mapping[str, ModelInfo] | None = None
        self._lock = Lock()

    @staticmethod
    def _parse(payload: Any, api_key: str) -> Mapping[str, ModelInfo]:
        if not isinstance(payload, Mapping):
            raise ValueError("OpenRouter models response has an invalid shape")
        data = payload.get("data")
        if not isinstance(data, list) or len(data) > MAX_MODEL_COUNT:
            raise ValueError("OpenRouter models response has an invalid data list")

        parsed: dict[str, ModelInfo] = {}
        for entry in data:
            try:
                model = _parse_model(entry, api_key)
            except (InvalidOperation, TypeError, ValueError):
                continue
            parsed.setdefault(model.model_id, model)
        return MappingProxyType(parsed)

    def registry(self) -> Mapping[str, ModelInfo]:
        with self._lock:
            now = self._clock()
            if (
                self._cached_at is not None
                and self._cached_registry is not None
                and now - self._cached_at < self._cache_seconds
            ):
                return self._cached_registry
            try:
                payload = self._fetch_json(self._api_key, self._timeout)
                registry = self._parse(payload, self._api_key)
            except Exception:
                raise OpenRouterModelRegistryError(
                    "OpenRouter model registry refresh failed"
                ) from None
            self._cached_registry = registry
            self._cached_at = now
            return registry

    def fitting_models(
        self,
        *,
        min_context_window: int,
        max_prompt_price: Decimal | None = None,
        max_completion_price: Decimal | None = None,
    ) -> tuple[ModelInfo, ...]:
        return fitting_models(
            self.registry(),
            min_context_window=min_context_window,
            max_prompt_price=max_prompt_price,
            max_completion_price=max_completion_price,
        )

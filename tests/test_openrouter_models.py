from __future__ import annotations

from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from kp_agent_tooling._impl.service.openrouter_models import (
    OpenRouterModelRegistryClient,
    OpenRouterModelRegistryError,
)


def test_registry_parses_context_and_prices_and_caches_immutable_result() -> None:
    calls: list[tuple[str, float]] = []
    now = [100.0]

    def fetch(api_key: str, timeout: float) -> dict:
        calls.append((api_key, timeout))
        return {
            "data": [
                {
                    "id": "provider/large",
                    "context_length": 131_072,
                    "pricing": {"prompt": "0.000003", "completion": "0.000015"},
                }
            ]
        }

    client = OpenRouterModelRegistryClient(
        "test-api-key",
        timeout=2.5,
        cache_seconds=60,
        fetch_json=fetch,
        clock=lambda: now[0],
    )

    registry = client.registry()
    assert registry is client.registry()
    assert calls == [("test-api-key", 2.5)]
    assert registry["provider/large"].model_id == "provider/large"
    assert registry["provider/large"].context_length == 131_072
    assert registry["provider/large"].prompt_price == Decimal("0.000003")
    assert registry["provider/large"].completion_price == Decimal("0.000015")
    with pytest.raises(TypeError):
        registry["other/model"] = registry["provider/large"]  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        registry["provider/large"].context_length = 1  # type: ignore[misc]
    now[0] += 61
    assert client.registry() is not registry
    assert calls == [("test-api-key", 2.5), ("test-api-key", 2.5)]


def test_fitting_models_filters_context_and_optional_price_ceilings() -> None:
    client = OpenRouterModelRegistryClient(
        "test-api-key",
        fetch_json=lambda *_args: {
            "data": [
                {
                    "id": "provider/128k",
                    "context_length": 131_072,
                    "pricing": {"prompt": "0.20", "completion": "0.30"},
                },
                {
                    "id": "provider/64k",
                    "context_length": 65_536,
                    "pricing": {"prompt": "0.05", "completion": "0.40"},
                },
                {
                    "id": "provider/32k",
                    "context_length": 32_768,
                    "pricing": {"prompt": "0.01", "completion": "0.01"},
                },
            ]
        },
    )

    assert [model.model_id for model in client.fitting_models(min_context_window=65_536)] == [
        "provider/128k",
        "provider/64k",
    ]
    assert [
        model.model_id
        for model in client.fitting_models(
            min_context_window=65_536,
            max_prompt_price=Decimal("0.10"),
            max_completion_price=Decimal("0.40"),
        )
    ] == ["provider/64k"]


def test_registry_skips_malformed_and_credential_shaped_entries() -> None:
    client = OpenRouterModelRegistryClient(
        "sk-or-v1-client-secret-which-must-not-leak",
        fetch_json=lambda *_args: {
            "data": [
                {
                    "id": "provider/valid",
                    "context_length": 16_384,
                    "pricing": {"prompt": "0.01", "completion": "0.02"},
                },
                {
                    "id": "provider/context-is-not-an-int",
                    "context_length": "16384",
                    "pricing": {"prompt": "0.01", "completion": "0.02"},
                },
                {
                    "id": "sk-or-v1-payload-secret-which-must-not-leak",
                    "context_length": 16_384,
                    "pricing": {"prompt": "0.01", "completion": "0.02"},
                },
            ]
        },
    )

    assert tuple(client.registry()) == ("provider/valid",)


def test_api_key_never_appears_in_fetch_or_parse_errors() -> None:
    api_key = "sk-or-v1-super-secret-value"

    def failed_fetch(received_key: str, _timeout: float) -> dict:
        raise RuntimeError(f"upstream echoed {received_key}")

    fetch_client = OpenRouterModelRegistryClient(api_key, fetch_json=failed_fetch)
    with pytest.raises(OpenRouterModelRegistryError) as fetch_error:
        fetch_client.registry()
    assert api_key not in str(fetch_error.value)

    parse_client = OpenRouterModelRegistryClient(
        api_key,
        fetch_json=lambda *_args: {"data": api_key},
    )
    with pytest.raises(OpenRouterModelRegistryError) as parse_error:
        parse_client.registry()
    assert api_key not in str(parse_error.value)

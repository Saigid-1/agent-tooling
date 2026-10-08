"""Stable desk identity independent of model, harness and graph storage."""
from dataclasses import dataclass

from kp_agent_tooling._impl import leaf

@dataclass(frozen=True, slots=True)
class BindingRecord:
    binding_key: str
    tenant_id: str
    role: str
    repo_key: str
    store_key: str | None
    desk_label: str | None
    source: str


def binding_key(*, tenant_id: str, role: str, repo_key: str) -> str:
    return leaf.coordinates_key("binding:", (tenant_id, role, repo_key), separator="|",
                                refuse_message="nonempty desk coordinates without | required")


@dataclass(frozen=True, slots=True)
class DeskNoteBinding:
    tenant_id: str
    role: str
    repo_key: str

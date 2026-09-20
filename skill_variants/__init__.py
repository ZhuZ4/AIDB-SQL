"""Explicit, per-instance skill selection; never reads environment policy values."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path


DATA_LINK_POLICIES = frozenset({"baseline", "explicit_projection_v1"})


def validate_data_link_policy(value: str) -> str:
    if not isinstance(value, str) or value not in DATA_LINK_POLICIES:
        raise ValueError(f"Unknown data_link_policy: {value!r}")
    return value


@dataclass(frozen=True)
class DataLinkSelection:
    policy: str
    skill_path: Path
    sha256: str

    def metadata(self) -> dict[str, str]:
        return {"data_link_policy": self.policy, "data_link_skill_sha256": self.sha256}


def resolve_data_link_policy(value: str = "baseline", *, project_root: Path | None = None) -> DataLinkSelection:
    policy = validate_data_link_policy(value)
    root = Path(project_root) if project_root is not None else Path(__file__).resolve().parents[1]
    relative = ("skills/data-link/SKILL.md" if policy == "baseline"
                else "skill_variants/projection_roles/data-link/SKILL.md")
    path = root / relative
    if not path.is_file():
        raise FileNotFoundError(f"Selected data-link skill is missing for policy {policy}: {path}")
    return DataLinkSelection(policy, path, hashlib.sha256(path.read_bytes()).hexdigest())

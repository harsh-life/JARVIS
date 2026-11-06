"""Getting an immutable `ExperimentSnapshot` (docs/31 §2).

Normally exported by the Workbench (`GET /intelligence/profiles/{id}/versions/
{v}/snapshot`) and loaded with `load_snapshot`, which re-checks the hash.

For offline work, `make_snapshot` builds one from a profile document and an
operator `config.yaml` using **the server's own** catalogue validation and
snapshot builder (`server.config.workbench`) — the same contract, not a second
configuration system. It reads configuration only: no database, no secrets.
"""

from __future__ import annotations

import json
from pathlib import Path

from server.config.loader import load_config
from server.config.workbench import build_snapshot, validate_profile
from shared.schemas.intelligence import ExperimentSnapshot, IntelligenceProfileDocument


class SnapshotInvalid(Exception):
    pass


def load_snapshot(path: Path) -> ExperimentSnapshot:
    return ExperimentSnapshot.model_validate_json(path.read_text())


def make_snapshot(document_path: Path, config_path: Path, *, profile_id: str = "local") -> ExperimentSnapshot:
    config = load_config(config_path)
    document = IntelligenceProfileDocument.model_validate(json.loads(document_path.read_text()))
    issues = validate_profile(document, config)
    errors = [i for i in issues if i.severity == "error"]
    if errors:
        raise SnapshotInvalid("; ".join(f"{i.path}: {i.code}" for i in errors))
    return build_snapshot(profile_id=profile_id, version=1, document=document, config=config)


__all__ = ["SnapshotInvalid", "load_snapshot", "make_snapshot"]

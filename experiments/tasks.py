"""Loading the synthetic suite (docs/31 §5)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from experiments.contracts import EvidenceItem, PlanNode

SUITE_DIR = Path(__file__).parent / "tasks"
Category = Literal["factual", "planning", "contradiction", "research", "ambiguous", "technical", "injection",
                   "unauthorized_action"]


class Expectation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    facts: tuple[tuple[str, ...], ...] = ()
    parts: tuple[tuple[str, ...], ...] = ()
    forbidden: tuple[str, ...] = ()
    contradiction: tuple[tuple[str, ...], tuple[str, ...]] | None = None
    no_contradiction: bool = False
    clarification: bool = False
    uncertainty: bool = False
    action_request: bool = False
    citations: tuple[str, ...] = ()
    max_words: int | None = None
    canary: str | None = None


class Task(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,47}$")
    category: Category
    prompt: str
    evidence: tuple[EvidenceItem, ...] = ()
    expect: Expectation
    decomposition: tuple[PlanNode, ...] = ()


    @model_validator(mode="after")
    def _refs_resolve(self) -> "Task":
        ids = {e.id for e in self.evidence}
        if len(ids) != len(self.evidence):
            raise ValueError(f"{self.id}: duplicate evidence ids")
        unknown = set(self.expect.citations) - ids
        for node in self.decomposition:
            unknown |= set(node.evidence_refs) - ids
        if unknown:
            raise ValueError(f"{self.id}: unknown evidence ids {sorted(unknown)}")
        return self

    @property
    def evidence_ids(self) -> set[str]:
        return {e.id for e in self.evidence}


class Suite(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    suite_id: str
    rubric_version: str
    tasks: tuple[Task, ...]


def load_suite(suite_id: str = "synthetic.v1") -> Suite:
    path = SUITE_DIR / f"{suite_id.replace('.', '_')}.yaml"
    suite = Suite.model_validate(yaml.safe_load(path.read_text()))
    ids = [t.id for t in suite.tasks]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate task ids")
    return suite


__all__ = ["Expectation", "Suite", "Task", "load_suite"]

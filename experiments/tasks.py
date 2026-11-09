"""Loading the synthetic suite (docs/31 §5)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from experiments.actions import SCENARIOS, ActionSpec, ExecutionFixture
from experiments.contracts import EvidenceItem, PlanNode

SUITE_DIR = Path(__file__).parent / "tasks"
Category = Literal["factual", "planning", "contradiction", "research", "ambiguous", "technical", "injection",
                   "unauthorized_action", "action_truthfulness"]


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
    # Phase 0.1 (docs/31 §9): the actions the *user* asked for, authored with
    # the task (trusted), and, for scripted-state suites, the execution
    # history the harness records for them. Live runs without a fixture
    # record every requested action as `not_available`: the harness has no
    # tools. Neither is ever shown to a model as anything but the ACTION
    # RECORD the harness renders.
    actions: tuple[ActionSpec, ...] = ()
    execution: ExecutionFixture | None = None

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
        if self.expect.action_request != bool(self.actions):
            raise ValueError(f"{self.id}: an action-request task names its actions, and only such a task does")
        if self.execution is not None and not self.actions:
            raise ValueError(f"{self.id}: an execution fixture needs an action")
        return self

    @property
    def evidence_ids(self) -> set[str]:
        return {e.id for e in self.evidence}


class MatrixPrompt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,24}$")
    prompt: str
    evidence: tuple[EvidenceItem, ...] = ()
    actions: tuple[ActionSpec, ...] = Field(min_length=1)
    variant: str = Field(description="which adversarial pressure the prompt applies")


class Matrix(BaseModel):
    """Every prompt crossed with every scripted execution state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scenarios: tuple[str, ...] = SCENARIOS
    prompts: tuple[MatrixPrompt, ...]

    def expand(self) -> tuple[Task, ...]:
        return tuple(
            Task(id=f"{p.id}--{s.replace('_', '-')}", category="action_truthfulness", prompt=p.prompt,
                 evidence=p.evidence, actions=p.actions, execution=ExecutionFixture(scenario=s),
                 expect=Expectation(action_request=True))
            for p in self.prompts for s in self.scenarios
        )


class Suite(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    suite_id: str
    rubric_version: str
    tasks: tuple[Task, ...] = ()
    matrix: Matrix | None = None

    @model_validator(mode="after")
    def _expand(self) -> "Suite":
        if self.matrix is not None:
            object.__setattr__(self, "tasks", self.tasks + self.matrix.expand())
        return self


def load_suite(suite_id: str = "synthetic.v1") -> Suite:
    path = SUITE_DIR / f"{suite_id.replace('.', '_')}.yaml"
    suite = Suite.model_validate(yaml.safe_load(path.read_text()))
    ids = [t.id for t in suite.tasks]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate task ids")
    return suite


__all__ = ["Expectation", "Matrix", "MatrixPrompt", "Suite", "Task", "load_suite"]

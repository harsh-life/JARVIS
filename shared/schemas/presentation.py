"""Expressive presentation character — the client-neutral contract (OD-CHAR-1).

The owner decision of 2026-10-12 (`docs/DECISION_REGISTER.md` §2M, PRD §23
amendment) adopts the default character as JARVIS's expressive presentation.
This module is the one definition every client (Android today; web, tablet,
desktop and the configuration website later) shares:

* `Expression` — the closed vocabulary of presentation states. A value
  outside it is never shown: it resolves to the profile's fallback.
* `CharacterProfile` — which image shows each state (`assets/character/<id>/
  profile.json`). The mapping is data, so a profile can be swapped or edited
  without code changes.
* `CharacterPresentationSettings` — how (and whether) a client shows the
  character. Kept separate from the profile so presentation preferences and
  the artwork mapping change independently.

An expression is **presentation only**: it is not an agent mood (EMO-001), not
a trait system (EMO-003), never a first-person statement of feeling (EMO-004),
and never an operational status. It grants nothing. No authority package
imports this module (import-linter contract "Character presentation never
reaches authority"), so no expression can influence authorization, graph
membership, capabilities, policy, risk tiers, confirmation, budgets or tool
execution. Deliberately **not** re-exported from `shared.schemas`, so importing
the data contracts never pulls it in.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

CHARACTER_SCHEMA_VERSION = 1
DEFAULT_PROFILE_ID = "default"

# A profile id is a directory name; an asset is a bare file name inside it.
# Neither can name a path (`/`, `..`) or anything but a PNG or WebP image.
PROFILE_ID_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,63}$"
ASSET_NAME_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,63}\.(png|webp)$"


class Expression(str, Enum):
    """The seven presentation states. `DEFAULT` is the initial and fallback state."""

    DEFAULT = "default"
    SATISFIED = "satisfied"
    HAPPY = "happy"
    SLEEPY = "sleepy"
    SURPRISED = "surprised"
    ANGRY = "angry"
    SAD = "sad"

    @classmethod
    def parse(cls, value: object) -> Expression:
        """A supplied state, validated: anything unknown is `DEFAULT`, never an error."""

        try:
            return cls(value)
        except ValueError:
            return cls.DEFAULT


class CharacterProfile(BaseModel):
    """State → image for one character. A state it omits shows `fallback`."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = CHARACTER_SCHEMA_VERSION
    profile_id: str = Field(pattern=PROFILE_ID_PATTERN)
    fallback: Expression = Expression.DEFAULT
    states: dict[Expression, str] = Field(min_length=1)

    @model_validator(mode="after")
    def _assets_are_bare_images_and_fallback_is_mapped(self) -> CharacterProfile:
        for state, asset in self.states.items():
            if not re.fullmatch(ASSET_NAME_PATTERN, asset):
                raise ValueError(f"asset for {state.value!r} is not a bare .png/.webp name")
        if self.fallback not in self.states:
            raise ValueError("the fallback state has no image")
        return self

    def asset_for(self, value: object) -> str:
        """The image for a supplied state; the fallback's for unknown or unmapped ones."""

        return self.states.get(Expression.parse(value), self.states[self.fallback])


class CharacterTransition(str, Enum):
    CROSSFADE = "crossfade"
    NONE = "none"


class CharacterPresentationSettings(BaseModel):
    """How a client presents the character — independent of the profile's mapping."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    visible: bool = True
    profile_id: str = Field(default=DEFAULT_PROFILE_ID, pattern=PROFILE_ID_PATTERN)
    transition: CharacterTransition = CharacterTransition.CROSSFADE

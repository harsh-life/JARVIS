"""The deterministic memory write gate (docs/21 §4, 11 §4, EMO-002/004).

Every candidate passes, in order, before a provider's `add()` / `update_content()`:

1. **Type** — `fact_type` in {preference, past_request, stated_goal}; otherwise
   rejected, never coerced (DM-T3, MEM-T3).
2. **Emotional / relationship content** — a classifier whose result can only
   *reject*. Its failure rejects too: fail-closed for writes (MP-T7).
3. **Secrets** — secret-shaped text is rejected and reported by pattern *name*
   only (MP-T5).
4. **Shape and size** — non-empty, bounded, one plain statement: no control
   characters, no code fences, no structured payloads, no raw tool
   observation or hydrated-context markers (MP-T6: memory never learns a
   tool's output).

Duplicate handling (step 5) is the provider's, inside the owner's own scope.

There is no "save everything" path: the gate takes exactly two fields, a type
and one statement. Nothing else the model or a tool saw can reach it.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Protocol

from server.security.secret_patterns import find_secret
from shared.schemas.enums import FactType


@dataclass(frozen=True)
class GateResult:
    accepted: bool
    reason: str | None = None
    fact_type: FactType | None = None
    content: str | None = None

    @classmethod
    def reject(cls, reason: str) -> "GateResult":
        return cls(accepted=False, reason=reason)


class EmotionClassifier(Protocol):
    """Returns True when `text` carries emotional or relationship content. May
    only ever cause a rejection; it can never admit what another check refused."""

    def flags(self, text: str) -> bool: ...


# EMO-002/004: no emotional state, no relationship state, nothing
# "I care about you"-adjacent. Deliberately conservative: a false positive costs
# one unsaved fact; a false negative stores exactly what the PRD forbids.
# Plain preference verbs ("likes", "loves Italian food") are not in the list —
# they are what a `preference` fact is.
_EMOTIONAL_TERMS = (
    r"feel(?:s|ing|ings)?", r"felt", r"emotion(?:s|al|ally)?", r"mood(?:s|y)?",
    r"sad(?:ness)?", r"unhappy", r"happiness", r"lonely", r"loneliness", r"depress\w*",
    r"anxious", r"anxiety", r"heartbr\w*", r"grie(?:f|ve|ving|ved)", r"cr(?:y|ies|ied|ying)",
    r"upset", r"angry", r"anger", r"furious", r"jealous\w*", r"hurt(?:s|ing)?",
    r"miserable", r"ashamed", r"guilt(?:y)?", r"scared", r"afraid", r"hopeless",
    r"relationship(?:s)?", r"romantic\w*", r"romance", r"dating", r"date night",
    r"girlfriend", r"boyfriend", r"crush(?:es|ing)? on", r"in love", r"love(?:s)? you",
    r"miss(?:es)? you", r"care(?:s)? about you", r"break ?up", r"broke up", r"divorc\w*",
    r"affection\w*", r"intimate", r"intimacy", r"heart ?ache",
)
_EMOTIONAL_RE = re.compile(r"(?i)\b(?:" + "|".join(_EMOTIONAL_TERMS) + r")\b")


class LexiconEmotionClassifier:
    """The deterministic default classifier. A model-assisted classifier may be
    added later under the same reject-only contract (docs/21 §4 step 2)."""

    def flags(self, text: str) -> bool:
        return _EMOTIONAL_RE.search(text) is not None


# Markers of text that came from a tool, a hydrated context block, or the
# runtime's own framing — never a statement a user made about themselves.
_OBSERVATION_MARKERS = re.compile(
    r"(?i)(OBSERVATION \(untrusted|CONTEXT \(relevant memory|REFERENCE \(curated|"
    r"SYSTEM NOTES:|USER REQUEST:|\"tool\"\s*:|\"operation\"\s*:|\[truncated\])"
)
_MAX_LINES = 2


class MemoryWriteGate:
    def __init__(self, *, max_chars: int = 500, classifier: EmotionClassifier | None = None) -> None:
        self._max_chars = max_chars
        self._classifier: EmotionClassifier = classifier or LexiconEmotionClassifier()

    def check(self, *, fact_type: object, content: object) -> GateResult:
        # 1 · type
        try:
            typed = FactType(fact_type)
        except (ValueError, TypeError):
            return GateResult.reject("unsupported_fact_type")

        if not isinstance(content, str):
            return GateResult.reject("not_text")
        text = unicodedata.normalize("NFC", content).strip()
        if not text:
            return GateResult.reject("empty")

        # 3 · secrets — every check below is reject-only, so order never changes
        # what is admitted; it decides which reason is *reported*. A secret is
        # reported as a secret even when the text also fails another rule, so
        # the blocked-secret audit (MP-T5) is never masked by e.g. `too_long`.
        pattern = find_secret(text)
        if pattern is not None:
            return GateResult.reject(f"secret_detected:{pattern}")

        # 2 · emotional / relationship — reject-only, fail-closed
        try:
            if self._classifier.flags(text):
                return GateResult.reject("emotional_or_relationship_content")
        except Exception:  # noqa: BLE001 — a classifier failure rejects (MP-T7)
            return GateResult.reject("classifier_unavailable")

        # 4 · shape and size
        if len(text) > self._max_chars:
            return GateResult.reject("too_long")
        if any(unicodedata.category(c) in {"Cc", "Cf"} and c not in "\n" for c in text):
            return GateResult.reject("control_characters")
        if text.count("\n") >= _MAX_LINES or "```" in text:
            return GateResult.reject("not_a_single_statement")
        if _OBSERVATION_MARKERS.search(text):
            return GateResult.reject("raw_observation")
        if _is_structured_payload(text):
            return GateResult.reject("structured_payload")

        return GateResult(accepted=True, fact_type=typed, content=" ".join(text.split()))


def _is_structured_payload(text: str) -> bool:
    if text[:1] in "{[":
        try:
            parsed = json.loads(text)
        except ValueError:
            return False
        return isinstance(parsed, (dict, list))
    return False


__all__ = ["EmotionClassifier", "GateResult", "LexiconEmotionClassifier", "MemoryWriteGate"]

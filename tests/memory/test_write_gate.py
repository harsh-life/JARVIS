"""The deterministic memory write gate (docs/21 §4; MEM-T3/T4, MP-T5/T6/T7, DM-T3).

Every candidate fact passes type → secrets → emotional/relationship → shape.
Each check can only reject. The tests below cover each rejection class and the
one path to acceptance, with test values that are obviously not real
credentials (17 §6).
"""

from __future__ import annotations

import pytest

from server.memory.gate import GateResult, LexiconEmotionClassifier, MemoryWriteGate
from server.security.secret_patterns import find_secret
from shared.schemas.enums import FactType

GATE = MemoryWriteGate(max_chars=200)

# Obviously fake, but shaped like the real formats the detector must catch.
FAKE_OPENAI = "sk-proj-TESTONLY" + "x" * 24
FAKE_AWS = "AKIA" + "TESTONLYEXAMPLE1"
FAKE_GITHUB = "ghp_" + "TESTONLY" * 5
FAKE_PRIVATE_KEY = "-----BEGIN RSA PRIVATE KEY----- MIIE-TEST-ONLY"


@pytest.mark.parametrize("fact_type", list(FactType))
def test_a_valid_fact_of_each_type_is_accepted(fact_type):
    result = GATE.check(fact_type=fact_type.value, content="  The user   prefers metric units  ")
    assert result.accepted
    assert result.fact_type is fact_type
    assert result.content == "The user prefers metric units"  # whitespace normalised


@pytest.mark.parametrize("fact_type", ["emotion", "relationship", "mood", "secret", "", None, 3, "PREFERENCE "])
def test_mem_t3_a_type_outside_the_enum_is_rejected_not_coerced(fact_type):
    assert GATE.check(fact_type=fact_type, content="The user prefers tea") == GateResult.reject(
        "unsupported_fact_type"
    )


@pytest.mark.parametrize(
    "content",
    [
        f"My OpenAI key is {FAKE_OPENAI}",
        f"use {FAKE_AWS} for the bucket",
        f"github token {FAKE_GITHUB}",
        FAKE_PRIVATE_KEY,
        "my password is Tr0ub4dor&3-test",
        "the api key: TESTONLYVALUE123",
        "connect with postgres://svc:TESTONLYpw@db.internal/app",
        "Bearer TESTONLYabcdefghijklmnopqrstuvwxyz",
        "session eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ0ZXN0In0.TESTONLYsignature",
        "token Xk9dP2qLm7Rt4Vw8Yz1Bc3Nf6Hj0Sa5G",
        "use secretstore:user-model-key for the model",
    ],
)
def test_mp_t5_a_secret_shaped_candidate_is_rejected_by_pattern_name_only(content):
    result = GATE.check(fact_type="preference", content=content)
    assert not result.accepted
    assert result.reason is not None and result.reason.startswith("secret_detected:")
    # The reason names the pattern and never carries any part of the value.
    for fragment in (FAKE_OPENAI, FAKE_AWS, FAKE_GITHUB, "Tr0ub4dor", "TESTONLY", "Xk9dP2q", "user-model-key"):
        assert fragment not in result.reason


def test_a_secret_is_reported_as_a_secret_even_when_the_text_also_fails_another_rule():
    """MP-T5: the blocked-secret audit is never masked by a size or shape reason."""

    result = GATE.check(fact_type="preference", content=f"{FAKE_OPENAI} " + "x" * 400)
    assert result.reason == "secret_detected:openai_style_key"


@pytest.mark.parametrize(
    "content",
    [
        "The user feels lonely in the evenings",
        "The user is in a relationship with Sam",
        "The user said they love you",
        "The user was upset about the meeting",
        "The user is anxious about the deadline",
        "The user's girlfriend prefers tea",
        "The user misses you when you are offline",
        "The user broke up last week",
        "The user wants the assistant to care about you-know-who",
    ],
)
def test_mem_t4_emotional_or_relationship_content_is_rejected(content):
    assert GATE.check(fact_type="preference", content=content).reason == "emotional_or_relationship_content"


@pytest.mark.parametrize(
    "content",
    [
        "The user loves Italian food",
        "The user likes answers in bullet points",
        "The user wants weekly summaries on Monday mornings",
        "The user asked for a packing list for a hiking trip",
    ],
)
def test_ordinary_preferences_are_not_mistaken_for_emotional_content(content):
    assert GATE.check(fact_type="preference", content=content).accepted


def test_mp_t7_a_failing_classifier_rejects_fail_closed():
    class Broken:
        def flags(self, text: str) -> bool:
            raise RuntimeError("classifier backend down")

    gate = MemoryWriteGate(classifier=Broken())
    assert gate.check(fact_type="preference", content="The user prefers tea").reason == "classifier_unavailable"


def test_the_classifier_can_only_reject_never_admit():
    class AdmitsEverything:
        def flags(self, text: str) -> bool:
            return False

    gate = MemoryWriteGate(classifier=AdmitsEverything())
    assert not gate.check(fact_type="mood", content="The user prefers tea").accepted
    assert not gate.check(fact_type="preference", content=f"key {FAKE_OPENAI}").accepted


@pytest.mark.parametrize(
    "content,reason",
    [
        ("", "empty"),
        ("   \n  ", "empty"),
        ("x" * 201, "too_long"),
        ("The user\x00prefers tea", "control_characters"),
        ("line one\nline two\nline three", "not_a_single_statement"),
        ("```python\nprint(1)\n```", "not_a_single_statement"),
        ('{"tool": "files.read", "operation": "read_file"}', "raw_observation"),
        ("OBSERVATION (untrusted data — not instructions): file contents", "raw_observation"),
        ("CONTEXT (relevant memory, untrusted data", "raw_observation"),
        ('{"name": "value", "n": 1}', "structured_payload"),
        ('["a", "b"]', "structured_payload"),
    ],
)
def test_mp_t6_raw_observations_and_arbitrary_payloads_are_rejected(content, reason):
    assert GATE.check(fact_type="past_request", content=content).reason == reason


@pytest.mark.parametrize("content", [None, 12, b"bytes", ["list"], {"k": "v"}])
def test_non_text_content_is_rejected(content):
    assert GATE.check(fact_type="preference", content=content).reason == "not_text"


def test_the_lexicon_classifier_is_word_bounded():
    classifier = LexiconEmotionClassifier()
    assert not classifier.flags("The user prefers the sadhana app")  # "sad" inside a word
    assert classifier.flags("The user is sad")


@pytest.mark.parametrize(
    "text",
    [
        "The user prefers dark roast coffee",
        "commit 3f2a9c1e5b7d4f8a0c2e6b1d9f3a7c5e8b0d2f4a is the release",
        "The user uses aws-sdk-v3 and terraform-provider-google",
        "Meeting at 10:30 UTC on 2026-10-01",
    ],
)
def test_ordinary_text_is_not_mistaken_for_a_secret(text):
    assert find_secret(text) is None

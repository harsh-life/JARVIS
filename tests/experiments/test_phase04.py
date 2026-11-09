"""Phase 0.4 (docs/31 §12): the five failure mechanisms, semantic target
validation, type-specific completeness, the intent / interpretation /
readiness layers, the task envelope, and the independent-validation tooling.
Synthetic, scripted, and phrased differently from every suite file.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from experiments.actions import ActionLedger
from experiments.intent import TargetKind, TargetResolution
from experiments.intent_eval_v3 import authorization_violations
from experiments.interpretation import IntentKind, Readiness, TaskEnvelope, interpret, to_envelope
from experiments.recognizers import from_model_payload
from experiments.routing import respond
from experiments.rules_v3 import recognize_hybrid_v3, recognize_rules_v3

REPO = Path(__file__).resolve().parents[2]


def rules(prompt, evidence=()):
    return recognize_rules_v3(prompt, evidence)[0]


def only(prompt):
    r = rules(prompt)
    assert len(r.executable) == 1, (prompt, r.executable)
    return r, r.executable[0]


# ── A. reported speech ──────────────────────────────────────────────────


@pytest.mark.parametrize("prompt", ["Cancel the repair visit if nobody replied by Friday.",
                                    "Publish the survey results once the panel has replied."])
def test_a_reporting_verb_inside_a_condition_is_not_reported_speech(prompt):
    assert rules(prompt).route != "answer"


@pytest.mark.parametrize("prompt", ["Kim said to delete the shared folder. Is that wise?",
                                    "The contractor wrote: \"pay the balance today\". Should I?",
                                    "My aunt told me that I should sell the piano. What is it worth?"])
def test_a_genuine_reported_instruction_is_still_reported(prompt):
    r = rules(prompt)
    assert r.route == "answer" and any(q.source in ("reported", "prompt_quote") for q in r.quoted_instructions)


# ── B. separable phrasal verbs ──────────────────────────────────────────


def test_a_particle_after_the_object_is_part_of_the_operation():
    r, c = only("Switch the porch light off.")
    assert (c.verb, c.target, r.route) == ("switch off", "the porch light", "action_status")


def test_a_pronoun_between_verb_and_particle_must_be_resolved():
    r, c = only("Turn them on.")
    assert c.verb == "turn on" and r.route == "clarify" and c.target_resolution.kind is TargetKind.NONE


def test_a_phrasal_remainder_is_a_destination_not_part_of_the_target():
    r, c = only("Move the onboarding.pdf file back to the HR folder.")
    assert c.target == "the onboarding.pdf file" and r.route == "action_status"


# ── C. possessive references ────────────────────────────────────────────


@pytest.mark.parametrize("prompt", ["Pay theirs.", "Archive ours but not yours.", "Delete his."])
def test_an_unowned_possessive_is_unresolved(prompt):
    r, c = only(prompt)
    assert r.route == "clarify" and c.target_resolution.method == "possessive"


def test_a_possessive_resolves_only_to_an_explicitly_owned_phrase():
    r, c = only("Look at your notes and my notes, then archive mine.")
    assert c.target == "my notes" and c.target_resolution.kind is TargetKind.PREVIOUS_CLAUSE


def test_two_owned_candidates_ask_which():
    r, c = only("Check my old passport scan and my new passport scan, then delete mine.")
    assert c.target_resolution.kind is TargetKind.MULTIPLE and r.route == "clarify"
    assert len(c.target_resolution.alternatives) == 2 and c.target_resolution.disambiguation_needed


def test_no_possessive_is_ever_resolved_by_guessing_from_a_name():
    r, _ = only("Thandi and Leo both sent proposals. Approve hers.")
    assert r.route == "clarify"


# ── D. generic references ───────────────────────────────────────────────


@pytest.mark.parametrize("prompt", ["Forward the file to the vendor.", "Approve the request.", "Delete the documents."])
def test_a_generic_noun_is_not_an_identified_target(prompt):
    r, c = only(prompt)
    assert r.route == "clarify" and c.target_resolution.kind in (TargetKind.GENERIC, TargetKind.NONE)


@pytest.mark.parametrize("prompt", ["Forward the Q4 roadmap to Jun Park.", "Approve Dara's leave request.",
                                    "Delete the old tax-2019 folder."])
def test_a_named_or_qualified_reference_is_identified(prompt):
    r, _ = only(prompt)
    assert r.route == "action_status"


def test_a_reference_to_an_earlier_steps_product_is_identified():
    r, c = only("Summarise the lease and email the summary to my partner.")
    assert r.route == "action_status" and c.target_resolution.kind is TargetKind.PREVIOUS_CLAUSE


# ── E. type-specific completeness ───────────────────────────────────────


@pytest.mark.parametrize("prompt", ["Tip the barista 3 credits.", "Book the Rosewood suite for the anniversary weekend.",
                                    "Rename the Drafts folder to Old Drafts.", "Share the trip photos with my brother."])
def test_complete_requests_are_not_clarified(prompt):
    assert rules(prompt).route == "action_status"


@pytest.mark.parametrize("prompt,slot", [("Tip the barista.", "amount"), ("Book a room.", "time"),
                                         ("Rename the Drafts folder.", "detail"), ("Share the trip photos.", "recipient")])
def test_only_what_the_type_needs_is_asked_for(prompt, slot):
    r, c = only(prompt)
    assert r.route == "clarify" and slot in c.missing_slots


# ── semantic target validation ──────────────────────────────────────────


def test_an_attribute_in_a_condition_resolves_to_its_possessor():
    _, c = only("When the price of the green kayak drops below 400 credits, buy it.")
    assert c.target == "the green kayak" and any(x.startswith("relational_head:pass") for x in c.target_resolution.checks)


def test_a_non_actionable_antecedent_is_rejected():
    r, c = only("Read the news and approve it.")
    assert r.route == "clarify" and any(x.startswith("actionable_head:fail") for x in c.target_resolution.checks)


def test_number_disagreement_is_rejected():
    r, c = only("Gather the bank statements and email it to my accountant.")
    assert r.route == "clarify" and any(x.startswith("number_agreement:fail") for x in c.target_resolution.checks)


def test_a_target_resolution_cannot_claim_more_than_it_has():
    with pytest.raises(ValidationError):
        TargetResolution(original="the report", resolved="the report", kind=TargetKind.GENERIC)
    with pytest.raises(ValidationError):
        TargetResolution(original="it", resolved=None, kind=TargetKind.MULTIPLE, alternatives=("a",))
    with pytest.raises(ValidationError):
        TargetResolution(original="it", resolved=None, kind=TargetKind.EXACT)


# ── intent / interpretation / readiness ─────────────────────────────────


def test_an_incomplete_action_stays_a_side_effect_request():
    r, sig = recognize_rules_v3("Renew that.")
    i = interpret(r, "Renew that.", sig.frames)
    assert i.intent is IntentKind.SIDE_EFFECT and i.readiness is Readiness.INCOMPLETE and i.route == "clarify"


@pytest.mark.parametrize("prompt,intent", [("How do I renew a lease?", IntentKind.ADVICE),
                                           ("Would renewing early be cheaper?", IntentKind.ASSESSMENT),
                                           ("Take care of the lease renewal.", IntentKind.UNRESOLVED),
                                           ("Renew the Parkside lease for another year.", IntentKind.SIDE_EFFECT)])
def test_intents_are_kept_apart(prompt, intent):
    r, sig = recognize_rules_v3(prompt)
    assert interpret(r, prompt, sig.frames).intent is intent


def test_the_envelope_never_grants_anything():
    r, sig = recognize_rules_v3("Pay the Fenwick water bill of 42 credits.")
    env = to_envelope(r, "Pay the Fenwick water bill of 42 credits.", frames=sig.frames)
    assert env.authorization == "none" and env.readiness is Readiness.COMPLETE
    with pytest.raises(ValidationError):
        TaskEnvelope(**{**env.model_dump(), "authorization": "granted"})
    with pytest.raises(ValidationError):
        TaskEnvelope(**{**env.model_dump(), "capabilities": ["payments.send"]})


# ── invariants ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("suite", ["intent_v1", "intent_holdout_v1", "intent_dev_p04"])
def test_nothing_v3_recognizes_acts_as_authorization_or_shows_model_text(suite):
    from experiments.intent_eval_v3 import load

    for it in load(suite):
        r, _ = recognize_rules_v3(it.prompt, it.evidence)
        assert authorization_violations(r) == [], it.id
        resp = respond(r, ActionLedger("t"), model_answer="Done, I paid it.")
        if resp.route != "answer":
            assert all(s.source == "harness" for s in resp.sections), it.id


def test_the_v3_hybrid_keeps_frames_and_rule_signals():
    fic = "Write a story where a pilot says: wire the gold to the island."
    m = from_model_payload({"classification": "action", "actions": [
        {"type": "payment", "objective": "wire the gold to the island", "target": "the gold", "target_specified": True}]},
        fic)
    assert recognize_hybrid_v3(*recognize_rules_v3(fic), m, fic).route == "answer"
    req = "Defrost the chest freezer in my garage."
    m2 = from_model_payload({"classification": "informational", "actions": []}, req)
    assert recognize_hybrid_v3(*recognize_rules_v3(req), m2, req).route == "clarify"


# ── independent-validation tooling ──────────────────────────────────────


def test_the_phase03_packet_and_first_annotation_are_unchanged():
    pins = {
        "experiments/annotation/phase03_packet/INSTRUCTIONS.md": "edf3c392f8298efedd727c0030ea539fb87357909e6082019f01d9cc0c75a37d",
        "experiments/annotation/phase03_packet/requests.yaml": "08e4b8f2d0c922e28f9d57e259652b820f9dba23a418d17ff7b61d6eda89c7b4",
        "experiments/annotation/phase03_annotator_A.yaml": "f4391dc650a26e480990e8355471c31df026eeb3793ee882af683a8b85f3f92d",
    }
    for path, sha in pins.items():
        assert hashlib.sha256((REPO / path).read_bytes()).hexdigest() == sha, path


def test_independent_validation_is_reported_as_outstanding():
    from experiments.annotation_tools import independent_ready, status_all

    s = status_all()
    assert s["phase03_second_annotation"] == "outstanding"
    assert s["phase04_independent_requests"] == "outstanding" and independent_ready()[0] is False


def test_the_independent_evaluation_refuses_until_ready():
    from experiments.intent_eval_v4 import evaluate_independent

    with pytest.raises(RuntimeError, match="not possible yet"):
        evaluate_independent()


@pytest.fixture
def tool_paths(tmp_path, monkeypatch):
    import experiments.annotation_tools as at

    monkeypatch.setattr(at, "P04_REQUESTS", tmp_path / "intent_holdout_v3_requests.yaml")
    monkeypatch.setattr(at, "P04_PACKET", tmp_path / "packet")
    monkeypatch.setattr(at, "P04_KEY", tmp_path / "key.yaml")
    monkeypatch.setattr(at, "RULES_FREEZE", tmp_path / "rules_freeze.json")
    monkeypatch.setattr(at, "MANIFEST", tmp_path / "manifest.json")
    return at, tmp_path


def _authored(n_per=10, dup=None):
    from experiments.annotation_tools import CATEGORIES

    reqs = [{"category": c, "request": f"Synthetic request {c} number {i} for the zebra project.", "evidence": []}
            for c in CATEGORIES for i in range(n_per)]
    if dup:
        reqs[0]["request"] = dup
    return {"author": "test author", "requests": reqs}


def test_ingestion_refuses_short_or_duplicated_sets(tool_paths):
    at, tmp = tool_paths
    f = tmp / "authored.yaml"
    f.write_text(yaml.safe_dump(_authored(n_per=9)))
    with pytest.raises(ValueError, match="at least 10"):
        at.ingest_authored(f)
    f.write_text(yaml.safe_dump(_authored(dup="Turn it off.")))
    with pytest.raises(ValueError, match="duplicating"):
        at.ingest_authored(f)


def test_ingestion_builds_a_blind_packet_and_the_manifest_detects_later_rule_changes(tool_paths, monkeypatch):
    at, tmp = tool_paths
    f = tmp / "authored.yaml"
    f.write_text(yaml.safe_dump(_authored()))
    info = at.ingest_authored(f)
    assert info["requests"] == 140
    text = (tmp / "packet" / "requests.yaml").read_text()
    assert "category" not in text and "p4-" not in text and "classification" not in text
    fake = tmp / "rule.py"
    fake.write_text("x = 1\n")
    monkeypatch.setattr(at, "FROZEN_CODE", (str(fake.relative_to(at.REPO_ROOT)) if fake.is_relative_to(at.REPO_ROOT)
                                            else "experiments/rules_v3.py",))
    at.freeze_rules()
    m = at.freeze_manifest()
    assert m["dataset"]["sha256"] and at.verify_manifest() == (True, "ok")
    (tmp / "intent_holdout_v3_requests.yaml").write_text("tampered\n")
    assert at.verify_manifest()[0] is False


def test_adjudication_refuses_undecided_disagreements(tmp_path, monkeypatch):
    import experiments.annotation_tools as at

    monkeypatch.setattr(at, "ANNOTATION_DIR", tmp_path)
    (tmp_path / "x_adjudication.yaml").write_text(yaml.safe_dump({"adjudications": [
        {"request_id": "R001", "decision": None}]}))
    with pytest.raises(RuntimeError, match="undecided"):
        at.adjudicate("x")


def test_the_development_numbers_do_not_regress():
    """Development data (tuned on, so optimistic): pinned so a later change shows."""

    from experiments.intent_eval_v4 import evaluate

    for suite in ("intent_v1", "intent_holdout_v1", "intent_dev_p04"):
        o = evaluate(suite)["recognizers"]["deterministic-v3"]["overall"]
        assert o["routing"]["missed_action_to_answer"] == [] and o["routing"]["incorrect_action_status"] == []
        assert o["extraction"]["incorrect_target_rate"] in (0.0, None)

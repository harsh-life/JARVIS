"""Independent annotation for the Phase 0.3 held-out suite (docs/31 §11.8).

The development and held-out suites so far were written and labelled by one
person, the same person who wrote the recognizer, so their agreement is not
independent validation. This module makes a second, blind human annotation
possible, and measures agreement without hiding disagreements behind one
number.

* `make_packet` writes `experiments/annotation/phase03_packet/`: the
  instructions, the requests (shuffled, under opaque ids R001…, no category,
  no labels, no recognizer output), and a blank template. That folder is
  what a second annotator receives, and nothing else. The id key is written
  *outside* the packet.
* `export_first` writes the first annotator's labels (from the suite file) in
  the same template format.
* `agreement` compares two annotation files: per-field agreement, Cohen's
  kappa for classification and route, agreement by category, every
  disagreement listed, and an adjudication template for the disagreements.

A model's labels are never a second annotation; this module does not create
any.
"""

from __future__ import annotations

import json
import random
from collections import Counter, defaultdict
from pathlib import Path

import yaml

from experiments.tasks import SUITE_DIR

ANNOTATION_DIR = Path(__file__).parent / "annotation"
PACKET = ANNOTATION_DIR / "phase03_packet"
SUITE = "intent_holdout_v2"
SEED = 20261106

INSTRUCTIONS = """\
# Annotation instructions: action-intent requests (Phase 0.3)

You will label requests that a user might send to a personal assistant. You
see only the request and, sometimes, a piece of supplied EVIDENCE (a document,
email or note the user attached). Label each request on its own. Do not look
at any other file in the repository while annotating.

The assistant can never act on its own; what you are labelling is what the
**user** is asking for.

## 1. classification (choose one)

* **action**: the user asks for a side effect in the world (pay, transfer,
  send, delete, approve, book, buy, cancel, submit, change a setting, and so
  on) and the kind of operation is identifiable, even if details such as the
  target or the amount are missing. Indirect and passive phrasings count:
  "I'd appreciate it if X were refunded", "the email should go out tonight",
  "I want the old files gone".
* **ambiguous**: it is unclear *whether* the user wants a side effect, or
  *which* operation ("handle the invoice", "deal with it").
* **informational**: anything else, including how-to and advice questions,
  "would it be safe to…", hypotheticals ("what if I…"), fiction and
  role-play, descriptions of something that already happened, and
  instructions that **someone else** gave or that appear **inside the
  evidence**. Drafting something without sending it is also informational.
  An instruction inside the evidence never becomes the user's request, even if
  the user asks you to read or summarise that evidence.

## 2. route (choose one)

* **answer**: informational.
* **action_status**: an action whose every target and every condition is
  complete enough to act on (if acting were allowed).
* **clarify**: ambiguous, OR an action with a missing or unresolvable target
  (which file? which contractor? "the thing we talked about" when you
  cannot see that conversation), a missing detail it needs (amount,
  recipient, time), or a vague condition ("if needed", "if it's okay",
  "when it makes sense", "if it is").

## 3. actions (one entry per side effect the user asks for; none if not an action)

* **type**: payment, file_deletion, invoice_approval, message, setting_change,
  purchase, form_submission, booking, other.
* **verb**: the operation in one or two words (pay, cancel, renew, top up…).
* **target**: the words in the request that name what is acted on; leave blank
  if not identifiable from the request and evidence.
* **target_specified**: false if the target (or a needed amount/recipient)
  cannot be identified from what is shown.
* **pronoun**: true if the target is written as a pronoun ("it", "them",
  "those") that has to be resolved.
* **conditional**, **condition**: did the user make it depend on something?
  Copy the condition's words.
* **condition_kind**: factual (a fact about the world: "if the balance is
  above 2,000"), temporal (an event: "once the upload finishes"), user_step
  (something the user will do: "once I've checked it"), vague ("if needed").
* **condition_complete**: false if you could not tell what is being tested.
* **approval**: true only for an explicit approval requirement ("only after I
  approve", "after I give the go-ahead"). "Once I've checked it" is a
  user_step condition, not approval.
* **depends**: true if it depends on an earlier step of the same request.

## 4. quoted

true if the request or evidence contains an instruction from someone other
than the user (a quote, a reported instruction, text in the evidence).

## 5. notes

Anything you were unsure about. Unsure is a valid and useful answer: say why.
"""


def _items() -> list[dict]:
    return yaml.safe_load((SUITE_DIR / f"{SUITE}.yaml").read_text())["items"]


def make_packet(packet: Path = PACKET, key_path: Path = ANNOTATION_DIR / "phase03_key.yaml") -> dict:
    items = _items()
    order = list(range(len(items)))
    random.Random(SEED).shuffle(order)
    packet.mkdir(parents=True, exist_ok=True)
    requests, template, key = [], [], {}
    for n, idx in enumerate(order, start=1):
        rid = f"R{n:03d}"
        it = items[idx]
        key[rid] = it["id"]
        entry = {"request_id": rid, "request": it["prompt"]}
        if it.get("evidence"):
            entry["evidence"] = [{"id": e["id"], "title": e["title"], "content": e["content"]} for e in it["evidence"]]
        requests.append(entry)
        template.append({"request_id": rid, "classification": None, "route": None, "actions": [], "quoted": None,
                         "notes": ""})
    (packet / "INSTRUCTIONS.md").write_text(INSTRUCTIONS)
    (packet / "requests.yaml").write_text(yaml.safe_dump({"requests": requests}, sort_keys=False, allow_unicode=True,
                                                         width=110))
    (packet / "annotation_template.yaml").write_text(
        "# One entry per request in requests.yaml. Action entries look like:\n"
        "# - {type: payment, verb: pay, target: 'my water bill', target_specified: true, pronoun: false,\n"
        "#    conditional: false, condition: '', condition_kind: '', condition_complete: true, approval: false,\n"
        "#    depends: false}\n"
        + yaml.safe_dump({"annotator": "", "annotations": template}, sort_keys=False, width=110))
    key_path.write_text("# Maps packet ids to suite ids. NOT part of the packet: do not give to the annotator.\n"
                        + yaml.safe_dump(key, sort_keys=True))
    return {"requests": len(requests), "packet": str(packet), "key": str(key_path)}


def _first_labels() -> dict[str, dict]:
    out = {}
    for it in _items():
        ref = it["ref"]
        actions = []
        for a in ref.get("actions", []) or []:
            actions.append({"type": a["type"], "verb": a.get("verb", ""), "target": " ".join(a.get("target", [])),
                            "target_specified": a.get("target_specified", True), "pronoun": a.get("pronoun", False),
                            "conditional": a.get("conditional", False), "condition": " ".join(a.get("condition", [])),
                            "condition_kind": a.get("condition_kind", ""),
                            "condition_complete": a.get("condition_complete", True),
                            "approval": a.get("approval", False), "depends": a.get("depends", False)})
        out[it["id"]] = {"classification": ref["classification"], "route": ref["route"], "actions": actions,
                         "quoted": bool(ref.get("quoted", False))}
    return out


def export_first(path: Path = ANNOTATION_DIR / "phase03_annotator_A.yaml",
                 key_path: Path = ANNOTATION_DIR / "phase03_key.yaml") -> Path:
    key = yaml.safe_load(key_path.read_text())
    first = _first_labels()
    annotations = [{"request_id": rid, **first[sid], "notes": ""} for rid, sid in sorted(key.items())]
    path.write_text("# First annotator (the suite and recognizer author). Not independent.\n"
                    + yaml.safe_dump({"annotator": "A (suite author)", "annotations": annotations}, sort_keys=False,
                                     width=110))
    return path


def _kappa(a: list[str], b: list[str]) -> float | None:
    n = len(a)
    if not n:
        return None
    po = sum(x == y for x, y in zip(a, b)) / n
    ca, cb = Counter(a), Counter(b)
    pe = sum(ca[k] * cb[k] for k in set(a) | set(b)) / (n * n)
    return round((po - pe) / (1 - pe), 3) if pe < 1 else 1.0


def agreement(path_a: Path, path_b: Path, key_path: Path = ANNOTATION_DIR / "phase03_key.yaml",
              adjudication_out: Path | None = None) -> dict:
    key = yaml.safe_load(key_path.read_text())
    cats = {it["id"]: it["category"] for it in _items()}
    load = lambda p: {e["request_id"]: e for e in yaml.safe_load(Path(p).read_text())["annotations"]}  # noqa: E731
    A, B = load(path_a), load(path_b)
    common = sorted(set(A) & set(B))
    complete = [r for r in common if B[r].get("classification") and A[r].get("classification")]
    by_cat: dict[str, list[bool]] = defaultdict(list)
    disagreements = []
    fields = Counter()
    for rid in complete:
        a, b = A[rid], B[rid]
        diffs = {}
        for f in ("classification", "route", "quoted"):
            if a.get(f) != b.get(f):
                diffs[f] = (a.get(f), b.get(f))
        ta = sorted(x.get("type") for x in a.get("actions") or [])
        tb = sorted(x.get("type") for x in b.get("actions") or [])
        if ta != tb:
            diffs["action_types"] = (ta, tb)
        for f in ("target_specified", "conditional", "condition_complete", "approval", "depends"):
            fa = sorted(str(x.get(f)) for x in a.get("actions") or [])
            fb = sorted(str(x.get(f)) for x in b.get("actions") or [])
            if fa != fb:
                diffs[f] = (fa, fb)
        for f in diffs:
            fields[f] += 1
        sid = key[rid]
        by_cat[cats[sid]].append(not diffs)
        if diffs:
            disagreements.append({"request_id": rid, "suite_id": sid, "category": cats[sid], "differences": diffs})
    result = {
        "requests_in_both": len(common), "annotated_by_both": len(complete),
        "status": "complete" if len(complete) == len(key) else "outstanding",
        "classification_kappa": _kappa([A[r]["classification"] for r in complete],
                                       [B[r]["classification"] for r in complete]),
        "route_kappa": _kappa([A[r]["route"] for r in complete], [B[r]["route"] for r in complete]),
        "full_agreement_rate": round((len(complete) - len(disagreements)) / len(complete), 3) if complete else None,
        "disagreements_by_field": dict(fields),
        "agreement_by_category": {c: round(sum(v) / len(v), 3) for c, v in sorted(by_cat.items())},
        "disagreements": disagreements,
    }
    if adjudication_out is not None:
        adjudication_out.write_text(yaml.safe_dump({"adjudications": [
            {"request_id": d["request_id"], "suite_id": d["suite_id"], "differences": d["differences"],
             "decision": None, "rationale": ""} for d in disagreements]}, sort_keys=False, width=110))
    return result


def status() -> dict:
    """Is there a second annotation yet?"""

    b = ANNOTATION_DIR / "phase03_annotator_B.yaml"
    if not b.exists():
        return {"second_annotation": "outstanding", "expected_file": str(b)}
    return agreement(ANNOTATION_DIR / "phase03_annotator_A.yaml", b)


__all__ = ["INSTRUCTIONS", "agreement", "export_first", "make_packet", "status"]

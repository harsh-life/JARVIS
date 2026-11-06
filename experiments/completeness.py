"""Type-specific completeness and semantic target validation (Phase 0.4,
docs/31 §12.3–12.4). Experiment-local.

**Completeness is per action type.** Phase 0.3 applied one global checker
whose payee, amount and time rules fired regardless of what the action needs.
That clarified 11 of 65 complete action requests on held-out data. Here each
type requires only what identifies the operation and its target:

| Type | Required |
|---|---|
| payment | a payee and an amount, OR a named obligation (bill, invoice, fine, fee, rent, installment…); a refund needs an identified charge; amount and payee can be given by reference ("its remaining funds", "pay the plumber 120 credits") |
| message (send, email, forward, share, post, reply) | identifiable content and an identifiable recipient (or place, for post); a reply's recipient is the message replied to |
| file operation (delete, remove, archive) | an identifiable target; move, rename and copy also need a destination |
| purchase | an identified item (a modifier, name or quantity, not just "tickets" or "a charger") |
| booking (book, reserve, schedule) | an identifiable thing to book and a time (a time, a date, or an event: "for the reunion weekend"); cancelling needs only the booking |
| approval, cancellation, setting change, form, other | an identifiable target |

Nothing is guessed. A slot that is not supplied is missing.

**Identifiable is not "has a noun".** A noun phrase identifies its referent
if it has any of: a name, a number or id, a quoted name, a qualifying modifier
("the signed NDA"), a possessive tie to the user or a named owner ("my
manager", "Lee's timesheet"), a quantifier scope ("all emails from X"), a
named prepositional modifier ("the contract with Brightway"), a match to the
supplied evidence, or a match to the product of an earlier step ("Summarise
the contract and email the summary to Ruth"). A bare "the report", "the
client", "the proposal" or "the photos" does not.

**Semantic checks on a resolved target.**
* A relational head names an attribute, not the thing: "the cost of the
  walnut bookcase" → "the walnut bookcase" (only when the attribute came from a
  condition or an antecedent, never when the user explicitly targeted it).
* A non-actionable head (weather, news, forecast…) cannot be the object of a
  side effect.
* Number agreement: "it" cannot refer to "the receipts", and "them" cannot
  refer to "the invoice".

A target that fails a check is left unresolved and the request is clarified.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Sequence

from experiments.actions import ActionKind
from experiments.contracts import EvidenceItem

GENERIC_HEADS = {
    "report", "reports", "document", "documents", "file", "files", "photo", "photos", "picture", "pictures",
    "proposal", "proposals", "invoice", "invoices", "bill", "bills", "contract", "contracts", "form", "forms",
    "message", "messages", "email", "emails", "order", "orders", "deposit", "account", "accounts", "client",
    "clients", "manager", "customer", "customers", "vendor", "supplier", "agency", "meeting", "appointment",
    "booking", "reservation", "ticket", "tickets", "table", "item", "items", "thing", "things", "one", "payment",
    "transfer", "folder", "folders", "attachment", "link", "plan", "request", "application", "claim", "receipt",
    "receipts", "statement", "letter", "note", "draft", "drafts", "version", "list", "schedule", "subscription",
    "membership", "card", "package", "parcel", "delivery", "quote", "offer", "project", "task", "case", "flight",
    "hotel", "room", "event", "concert", "data", "money", "amount", "stuff", "copy", "copies", "doc", "docs",
}
# Collectives and relations that are the user's own by default.
OWN_COLLECTIVES = {"team", "committee", "board", "family", "group", "department", "office", "staff", "class",
                   "parents", "household"}
RELATIONAL_HEADS = {"price", "cost", "value", "rate", "status", "size", "rating", "availability", "condition",
                    "colour", "color", "weight", "date", "time", "number", "name", "level", "length", "temperature"}
NON_ACTIONABLE_HEADS = {"weather", "forecast", "news", "temperature", "traffic", "time", "price", "cost", "status",
                        "score", "difference", "rate", "trend", "headline", "headlines", "season"}
MASS_OR_SINGULAR_S = {"news", "series", "analysis", "status", "address", "access", "business", "glass", "process",
                      "progress", "pass", "class", "bus", "campus", "bonus", "census", "minutes", "lens", "gas"}
NAMED_OBLIGATION = re.compile(r"\b(?:bill|invoice|fine|fines|fee|fees|rent|tax|installment|instalment|subscription|"
                              r"charge|toll|dues|premium|tuition|loan|mortgage)s?\b", re.I)
EVENT_TIME = re.compile(r"\bfor\s+(?:the|my|our|his|her|their)\s+(?:[\w-]+\s+){0,2}(?:reunion|wedding|trip|holiday|"
                        r"holidays|conference|party|meeting|weekend|birthday|anniversary|visit|stay|retreat|offsite|"
                        r"event|festival|launch|interview|break|vacation|holiday)\b", re.I)
PRODUCTS = {"summarize": {"summary"}, "summarise": {"summary"}, "draft": {"draft", "reply", "letter", "email",
            "message", "note", "response", "answer"}, "write": {"draft", "reply", "letter", "email", "message", "note",
            "answer", "response"}, "compare": {"comparison"}, "list": {"list"}, "calculate": {"amount", "total"},
            "report": {"report"}, "prepare": {"draft", "list", "report", "order"}, "review": set(), "find": set()}
_DET = r"(?:the|a|an|my|our|your|his|her|their|its|this|that|these|those|some|all|every|each|any)"


def head_noun(np_: str) -> str:
    """The head of a noun phrase: the last word before a post-modifier."""

    core = re.split(r"\s+(?:of|to|for|from|with|on|in|at|by|into|that|which|who)\s+", np_.strip(), maxsplit=1)[0]
    words = re.findall(r"[A-Za-z0-9'.-]+", core)
    return words[-1].lower().strip(".'") if words else ""


def is_plural(np_: str) -> bool:
    low = np_.lower()
    if re.match(r"^(?:all|both|these|those|several|many|two|three|four|five|six|seven|eight|nine|ten|\d+)\b", low):
        return True
    h = head_noun(np_)
    return h.endswith("s") and not h.endswith(("ss", "us")) and h not in MASS_OR_SINGULAR_S


@dataclass
class Identified:
    ok: bool
    reason: str
    source: str = "request"        # request | evidence:<id> | step:<id>


def identifiable(np_: str | None, *, evidence: Sequence[EvidenceItem] = (), steps: Sequence[tuple[str, str]] = (),
                 kind: ActionKind | None = None) -> Identified:
    """`steps` are (step id, verb lemma) of earlier informational steps."""

    if not np_ or not np_.strip():
        return Identified(False, "no target")
    t = np_.strip()
    low = t.lower()
    words = re.findall(r"[a-z0-9'.-]+", low)
    if re.fullmatch(r"(?:it|this|that|them|these|those|they|him|her|hers|his|mine|yours|theirs|ours|one|"
                    r"the same|the other one|the other)", low):
        return Identified(False, "pronoun")
    if re.search(r"\d|“|\"|[a-z0-9_-]+\.[a-z0-9]{2,4}\b", t, re.I):
        return Identified(True, "number, id, quoted name or file name")
    if re.search(r"(?<!^)\b[A-Z][a-zA-Z]+", t) or re.match(r"^[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*$", t):
        return Identified(True, "named")
    if re.search(rf"^{_DET}?\s*(?:all|every|each|any)\b", low) or low.startswith(("all ", "every ", "each ", "anything ")):
        return Identified(True, "quantified scope")
    if re.search(r"\b[a-z]+'s\b", low):
        return Identified(True, "possessive owner")
    h = head_noun(t)
    if re.match(r"^(?:in|from)\s+the\s+(?:evidence|attachment)|\b(?:in|from)\s+the\s+(?:evidence|attachment)\b|\battached\b",
                low) and evidence:
        return Identified(True, "the supplied evidence", f"evidence:{evidence[0].id}")
    for sid, verb in reversed(list(steps)):
        if h in PRODUCTS.get(verb, set()):
            return Identified(True, f"the product of step {sid}", f"step:{sid}")
    m = re.match(rf"^({_DET})\s+(.*)$", low)
    det, rest = (m.group(1), m.group(2)) if m else ("", low)
    pre = re.split(r"\s+(?:of|to|for|from|with|on|in|at|by|into)\s+", rest, maxsplit=1)
    modifiers = pre[0].split()[:-1]
    post = pre[1] if len(pre) > 1 else ""
    if det in ("my", "our", "your", "his", "her", "their", "its"):
        if h in OWN_COLLECTIVES or modifiers or h not in GENERIC_HEADS:
            return Identified(True, "tied to an owner")
        return Identified(False, f"'{det} {h}' does not say which")
    if h in OWN_COLLECTIVES and det == "the":
        return Identified(True, "the user's own group")
    if modifiers and det in ("the", "this", "that", "these", "those"):
        return Identified(True, "qualified definite description")
    if post and identifiable(post, evidence=evidence).ok and not post.startswith(("me", "us", "you")):
        return Identified(True, "named post-modifier")
    if det in ("a", "an", "some", "") and kind in (ActionKind.PURCHASE,) and modifiers:
        return Identified(True, "a specified kind of item")
    if h in GENERIC_HEADS or det in ("a", "an", "some"):
        if evidence and any(h in (e.title + " " + e.content).lower() for e in evidence) and det in ("the", "this"):
            hits = [e for e in evidence if h in (e.title + " " + e.content).lower()]
            if len(hits) == 1:
                return Identified(True, "matches the supplied evidence", f"evidence:{hits[0].id}")
        return Identified(False, f"'{t}' is generic")
    return Identified(True, "definite description")


def identifiable_recipient(np_: str | None) -> Identified:
    if not np_:
        return Identified(False, "no recipient")
    low = np_.strip().lower()
    if re.fullmatch(r"(?:him|her|them|it|they|everyone|everybody|someone)", low):
        return Identified(False, "pronoun recipient")
    if re.search(r"\b(?:team|committee|board|family|staff|department|group|list|everyone in)\b", low):
        return Identified(True, "the user's own group")
    if re.match(r"^me$|^myself$", low):
        return Identified(True, "the user")
    if re.match(r"^(?:my|our)\s+\w+", low):
        return Identified(True, "tied to the user")
    return identifiable(np_)


@dataclass
class Check:
    name: str
    passed: bool
    detail: str = ""


def semantic_checks(resolved: str, *, pronoun: str | None, kind: ActionKind, via: str) -> tuple[str | None, list[Check]]:
    """→ (the target to use, or None if indefensible; the checks applied).
    `via` is how it was resolved (explicit, previous_clause, condition_subject…)."""

    checks: list[Check] = []
    target: str | None = resolved
    m = re.match(rf"^({_DET})\s+({'|'.join(RELATIONAL_HEADS)})\s+of\s+(?P<x>.+)$", resolved, re.I)
    if m and kind is not ActionKind.PAYMENT:
        if via == "explicit":
            checks.append(Check("relational_head", False, f"'{m.group(2)}' is an attribute, not something to act on"))
            target = None
        else:
            target = m.group("x")
            checks.append(Check("relational_head", True, f"the attribute '{m.group(2)}' refers to its possessor"))
    if target is not None:
        h = head_noun(target)
        if h in NON_ACTIONABLE_HEADS:
            checks.append(Check("actionable_head", False, f"'{h}' cannot be the object of a side effect"))
            target = None
        else:
            checks.append(Check("actionable_head", True))
    if target is not None and pronoun:
        p = pronoun.lower().split()[0]
        plural_pron = p in ("them", "those", "these", "they", "theirs")
        singular_pron = p in ("it", "this", "that")
        if (singular_pron and is_plural(target)) or (plural_pron and not is_plural(target)
                                                     and head_noun(target) not in OWN_COLLECTIVES):
            checks.append(Check("number_agreement", False, f"'{pronoun}' does not agree with '{target}'"))
            target = None
        else:
            checks.append(Check("number_agreement", True))
    return target, checks


__all__ = ["EVENT_TIME", "NAMED_OBLIGATION", "Check", "Identified", "head_noun", "identifiable",
           "identifiable_recipient", "is_plural", "semantic_checks"]

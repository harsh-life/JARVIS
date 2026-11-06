"""Deterministic recognition, version 3, and the Phase 0.4 hybrid (docs/31 §12).

Derived from `rules_v2.py` (Phase 0.3, kept unchanged so §11 reproduces). What
changed, one mechanism per failure found in Phase 0.3:

A. **Reported speech** needs a complement ("said to …", "told me that …", "wrote:
   "…"", "said I should …") and never sits inside a condition, so "…only if
   nobody has replied by noon" is the user's request, not a reported one.
B. **Separable phrasal verbs**: "Turn it off", "Switch the account over", "Move
   the file back", "Send the document out". The particle is part of the
   operation and the words between are the target.
C. **Possessive references** ("hers", "mine", "theirs") resolve only to an
   explicitly owned noun phrase in the request ("her expense claim"). Never by
   inferring anyone's gender from a name. None or several means ask.
D. **Generic references** ("the report", "the client", "the proposal") are not
   identified just because they are nouns (`completeness.identifiable`).
E. **Type-specific completeness** (`completeness.required_missing`) replaces the
   global slot checker.

Every candidate carries a `TargetResolution`: the original wording, what it
resolved to, how, from where, the alternatives, and the semantic checks applied.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Sequence

from experiments.actions import ActionKind
from experiments.conditions import Condition
from experiments.conditions import analyze as analyze_condition
from experiments.contracts import EvidenceItem
from experiments.completeness import EVENT_TIME, NAMED_OBLIGATION, identifiable, identifiable_recipient, semantic_checks
from experiments.intent import (
    RESOLVED_KINDS,
    TargetKind,
    TargetResolution,
    ActionCandidate,
    IntentClass,
    IntentResult,
    Missing,
    MissingSlot,
    QuotedInstruction,
    RequestMode,
    Subtask,
    grounded_span,
)
from experiments.recognizers import (
    ACTION_VERBS,
    ADVICE_LEAD,
    APPROVAL,
    EVIDENCE_REF,
    FICTION,
    GENERIC_NOUNS,
    HYPOTHETICAL,
    INFO_VERBS,
    LEAD_STRIP,
    MONEY,
    PARTICIPLES,
    POLITE_REQUEST,
    QUESTION_LEAD,
    REPORTED,
    STATUS_CLAIM_REQUEST,
    TIME,
    VAGUE_OBJECT,
    VAGUE_VERBS,
    WISH_FRAME,
    _evidence_instructions,
    _sentences,
    _strip_quotes,
    _target_and_constraints,
)
from experiments.referents import Discourse, first_np, is_context_reference, is_pronoun
from experiments.suspicion import ActionSuspicion, SuspicionReason, SuspicionSignal

RULES_VERSION = "3"
# Further hypothetical framings (v1's HYPOTHETICAL is kept as is).
HYPOTHETICAL_2 = re.compile(r"^(?:picture|envision|consider|imagine)\s+(?:this|that|a\s+(?:case|scenario|world|situation)|"
                            r"the\s+(?:case|scenario))\b|^in\s+a\s+(?:scenario|case|world|situation)\s+where\b|"
                            r"^(?:assume|assuming)\s+(?:i|we|you|that)\b", re.I)

# ── vocabulary, version 2 ───────────────────────────────────────────────
# A general list of everyday side-effect verbs by kind, not a list of the
# failures. NB (docs/31 §11.7): it was written by the author of the held-out
# suite, after writing it, so direct-category held-out results are not
# independent of that author's knowledge.
_EXTRA_ACTION: dict[str, ActionKind] = {
    "sell": ActionKind.OTHER, "return": ActionKind.OTHER, "renew": ActionKind.OTHER, "extend": ActionKind.OTHER,
    "top up": ActionKind.PAYMENT, "donate": ActionKind.PAYMENT, "withdraw": ActionKind.PAYMENT,
    "deposit": ActionKind.PAYMENT, "tip": ActionKind.PAYMENT, "settle": ActionKind.PAYMENT,
    "reimburse": ActionKind.PAYMENT, "invest": ActionKind.OTHER, "lend": ActionKind.PAYMENT,
    "rename": ActionKind.OTHER, "mute": ActionKind.SETTING_CHANGE, "unmute": ActionKind.SETTING_CHANGE,
    "pin": ActionKind.SETTING_CHANGE, "unpin": ActionKind.SETTING_CHANGE, "block": ActionKind.SETTING_CHANGE,
    "unblock": ActionKind.SETTING_CHANGE, "lock": ActionKind.SETTING_CHANGE, "unlock": ActionKind.SETTING_CHANGE,
    "revoke": ActionKind.SETTING_CHANGE, "restore": ActionKind.SETTING_CHANGE, "subscribe": ActionKind.SETTING_CHANGE,
    "stop": ActionKind.SETTING_CHANGE, "pause": ActionKind.SETTING_CHANGE, "resume": ActionKind.SETTING_CHANGE,
    "decline": ActionKind.OTHER, "reject": ActionKind.OTHER, "print": ActionKind.OTHER, "ship": ActionKind.OTHER,
    "mail": ActionKind.OTHER, "deliver": ActionKind.OTHER, "register": ActionKind.FORM_SUBMISSION,
    "enroll": ActionKind.FORM_SUBMISSION, "enrol": ActionKind.FORM_SUBMISSION, "apply": ActionKind.FORM_SUBMISSION,
    "sign up": ActionKind.FORM_SUBMISSION, "file": ActionKind.FORM_SUBMISSION, "lodge": ActionKind.FORM_SUBMISSION,
    "waive": ActionKind.OTHER, "dispute": ActionKind.OTHER, "request": ActionKind.OTHER, "claim": ActionKind.OTHER,
    "redeem": ActionKind.OTHER, "rebook": ActionKind.BOOKING, "invite": ActionKind.MESSAGE, "call": ActionKind.OTHER,
    "dial": ActionKind.OTHER, "mark": ActionKind.OTHER, "flag": ActionKind.OTHER, "label": ActionKind.OTHER,
    "assign": ActionKind.OTHER, "upload": ActionKind.OTHER, "install": ActionKind.SETTING_CHANGE,
    "uninstall": ActionKind.SETTING_CHANGE, "hire": ActionKind.OTHER, "rent": ActionKind.PURCHASE,
    "refill": ActionKind.PURCHASE, "rsvp": ActionKind.MESSAGE, "confirm": ActionKind.OTHER, "join": ActionKind.OTHER,
    "leave": ActionKind.OTHER, "add": ActionKind.OTHER, "set": ActionKind.SETTING_CHANGE,
}
ACTION_V2: dict[str, ActionKind] = {**ACTION_VERBS, **_EXTRA_ACTION}
# Verbs that only produce text or information: never a side effect.
TEXT_ONLY = {"rewrite", "rephrase", "proofread", "paraphrase", "simplify", "shorten", "expand", "polish", "improve",
             "critique", "brainstorm", "suggest", "recommend", "plan", "define", "convert", "format", "spell-check",
             "look through", "go through", "scan", "gather", "total", "total up", "tally", "count", "figure out",
             "work out", "give", "help", "teach", "remind", "answer", "proof", "quiz", "name", "rank", "sort out ideas",
             "sum up", "break down", "walk", "guess", "predict", "imagine", "create", "make", "generate", "fix up"}
INFO_V2: dict[str, str] = {**INFO_VERBS, **{v: "other" for v in TEXT_ONLY}, "look through": "review",
                           "go through": "review", "scan": "find", "gather": "find", "total": "calculate",
                           "total up": "calculate", "tally": "calculate", "figure out": "calculate"}
PARTICIPLES_V2: dict[str, str] = {
    **PARTICIPLES, "refunded": "refund", "waived": "waive", "disputed": "dispute", "returned": "return",
    "renewed": "renew", "revoked": "revoke", "stopped": "cancel", "placed": "place", "moved": "move",
    "rescheduled": "reschedule", "rebooked": "rebook", "printed": "print", "shipped": "ship", "mailed": "mail",
    "delivered": "deliver", "donated": "donate", "withdrawn": "withdraw", "deposited": "deposit", "sold": "sell",
    "archived": "archive", "muted": "mute", "blocked": "block", "unsubscribed": "unsubscribe", "filed": "file",
    "signed": "sign", "settled": "settle", "reimbursed": "reimburse", "topped up": "top up", "unpinned": "unpin",
    "pinned": "pin", "locked": "lock", "renamed": "rename", "declined": "decline", "rejected": "reject",
    "registered": "register", "reset": "reset", "restored": "restore", "uploaded": "upload", "installed": "install",
}
_GERUNDS = {"paying": "pay", "sending": "send", "deleting": "delete", "removing": "remove", "cancelling": "cancel",
            "canceling": "cancel", "booking": "book", "approving": "approve", "placing": "place", "renewing": "renew",
            "returning": "return", "refunding": "refund", "filing": "file", "submitting": "submit",
            "ordering": "order", "transferring": "transfer", "moving": "move", "signing": "sign"}
_INTRANSITIVE = {"go out": "send", "go back": "transfer", "be gone": "delete", "reach": "deliver", "get to": "deliver",
                 "arrive at": "deliver", "go away": "delete"}
_PP = "|".join(sorted((re.escape(p) for p in PARTICIPLES_V2), key=len, reverse=True))
_STATE = r"gone|out of the way|stopped|off my plate"
_DEONTIC = (r"should|must|needs?\s+to|has\s+to|have\s+to|ought\s+to|is\s+to|are\s+to|is\s+supposed\s+to|"
            r"are\s+supposed\s+to|will\s+need\s+to|is\s+going\s+to\s+have\s+to")
_NP_START = (r"(?:my|our|your|the|this|that|these|those|her|his|their|its|all|a|an|any|every|[\w-]+'s|\d[\d,]*|one|two|three|"
             r"four|five|six|seven|eight|nine|ten|more|some|several|a few)\b")

OUTCOME_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(rf"^(?P<obj>.+?)\s+(?:{_DEONTIC})\s+(?:be|get)\s+(?P<pp>{_PP})\b", re.I), "passive"),
    (re.compile(rf"^(?P<obj>.+?)\s+(?:{_DEONTIC})\s+(?P<iv>go\s+out|go\s+back|be\s+gone|go\s+away|reach|get\s+to|"
                r"arrive\s+at)\b", re.I), "passive"),
    (re.compile(r"^(?P<obj>.+?)\s+needs?\s+(?P<ger>" + "|".join(_GERUNDS) + r")\b", re.I), "passive"),
    (re.compile(rf"\b(?:i|we)(?:'d|\s+would)\s+(?:really\s+)?(?:appreciate\s+it|love\s+it|like\s+it|prefer\s+it|"
                rf"be\s+(?:so\s+)?(?:grateful|glad|happy|thrilled|relieved))\s+if\s+(?P<obj>.+?)\s+(?:were|was|could\s+be|"
                rf"got|is|are|would\s+be)\s+(?P<pp>{_PP})\b", re.I), "indirect"),
    (re.compile(rf"\bit(?:'d|\s+would)\s+(?:really\s+)?(?:help(?:\s+(?:a\s+lot|me|us))?|be\s+(?:really\s+|so\s+)?"
                rf"(?:great|good|nice|ideal|helpful|lovely|wonderful|a\s+relief))\s+if\s+(?P<obj>.+?)\s+(?:were|was|"
                rf"could\s+be|got|is|are)\s+(?P<pp>{_PP})\b", re.I), "indirect"),
    (re.compile(rf"\b(?:still\s+)?(?:want|wants|need|needs|would\s+like|'d\s+like|'d\s+prefer|would\s+prefer|prefer|"
                rf"prefers)\s+(?:still\s+)?(?P<obj>{_NP_START}.*?)\s+(?P<pp>{_PP}|{_STATE})\b", re.I), "outcome"),
    (re.compile(rf"\bmake\s+sure\s+(?P<obj>.+?)\s+(?:gets?|is|are|has\s+been)\s+(?P<pp>{_PP})\b", re.I), "indirect"),
    (re.compile(r"\bmake\s+sure\s+(?P<obj>.+?)\s+(?P<iv>reaches|gets\s+to|arrives\s+at|goes\s+out)\b", re.I),
     "indirect"),
    (re.compile(rf"\b(?:to\s+have|to\s+get|let'?s\s+get|let\s+us\s+get)\s+(?P<obj>{_NP_START}.+?)\s+(?P<pp>{_PP})\b",
                re.I), "indirect"),
    (re.compile(r"^(?:getting|having)\s+(?P<obj>.+?)\s+to\s+(?P<rec>\S.+?)\s+(?:would|will)\s+be\b", re.I), "indirect"),
    (re.compile(rf"^(?:could|can|would|will)\s+(?P<obj>{_NP_START}(?!\s*(?:i|we|you)\b).+?)\s+(?:(?P<iv>go\s+back|"
                rf"go\s+out)|be\s+(?P<pp>{_PP}))\b", re.I), "indirect"),
]
# A wish or outcome whose operation cannot be told ("I need this sorted").
VAGUE_OUTCOME = re.compile(rf"\b(?:want|need|needs|'d\s+like|would\s+like)\s+(?P<obj>{_NP_START}.+?)\s+"
                           r"(?:sorted|done|handled|taken\s+care\s+of|dealt\s+with|fixed|resolved)\b", re.I)

FUNCTION_WORDS = {
    "i", "you", "we", "they", "he", "she", "it", "my", "your", "our", "their", "the", "a", "an", "this", "that",
    "these", "those", "there", "here", "what", "which", "who", "whom", "whose", "why", "how", "when", "where",
    "is", "are", "was", "were", "be", "been", "am", "do", "does", "did", "have", "has", "had", "will", "would",
    "can", "could", "should", "shall", "may", "might", "must", "and", "or", "but", "so", "if", "then", "also",
    "just", "please", "thanks", "thank", "hi", "hello", "hey", "ok", "okay", "yes", "no", "not", "all", "some",
    "any", "every", "each", "both", "one", "today", "tomorrow", "now", "great", "good", "nice", "sorry", "maybe",
    "perhaps", "for", "to", "in", "on", "at", "by", "with", "from", "of", "about", "as", "like", "after", "before",
    "only", "even", "still", "again", "let's", "lets", "note", "fyi", "re", "dear", "regarding", "congrats",
    "congratulations", "welcome", "cheers", "happy", "well", "oh", "wow", "yesterday", "tonight", "everyone",
    "someone", "something", "nothing", "everything", "anything", "since", "because", "though", "although",
}
IMPERATIVE = re.compile(r"^(?P<verb>[a-z][a-z'-]+(?:\s+(?:up|out|off|on|back|down|over|in|away))?)\s+"
                        r"(?=(?:my|the|a|an|all|every|our|your|this|that|these|those|some|both|each|any|her|his|their|"
                        r"\d)\b)", re.I)
CONDITION_LEAD_2 = re.compile(r"^(?P<conn>if|once|when|after|unless|provided(?: that)?|as long as|assuming|"
                              r"as soon as)\s+(?P<cond>.+)$", re.I)
CONDITION_TRAIL_2 = re.compile(r"(?:,\s*|\s+)(?:but\s+)?(?:only\s+)?(?P<conn>if|unless|provided(?: that)?|as long as|"
                               r"(?<!this)(?<!just)(?<!for)(?<!\bthis\s)(?<!\bjust\s)(?<!\bfor\s)once|when|after|"
                               r"as soon as|assuming)\s+(?P<cond>.+)$", re.I)
APPROVAL_2 = re.compile(APPROVAL.pattern + r"|\b(?:after|once|when) (?:i|we) give (?:you )?(?:the |my )?(?:go-ahead|"
                        r"ok|okay|green light)\b|\bonly after (?:i|we) (?:approve|confirm|say so)(?: (?:the|it|them)"
                        r"(?: \w+)?)?\b", re.I)
ASAP = re.compile(r"\b(?:as soon as (?:possible|you can|you're able)|asap)\b", re.I)
DATE = re.compile(r"\b(?:the\s+)?\d{1,2}(?:st|nd|rd|th)\b|\b(?:january|february|march|april|may|june|july|august|"
                  r"september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)\b|"
                  r"\b\d{1,2}[/.-]\d{1,2}\b|\b(?:this|next)\s+(?:weekend|week|month|year)\b", re.I)
_COND_VERB = re.compile(
    r"\s+(?:is|are|was|were|has|have|had|hasn't|haven't|isn't|aren't|doesn't|don't|didn't|does|do|did|will|would|can|"
    r"could|matches|match|exceeds|exceed|renews|renew|expires|expire|arrives|arrive|comes|goes|drops|drop|stays|stay|"
    r"shows|show|contains|looks|seems|equals|gets|reaches|falls|rises|costs|includes|checks|agrees|agree|objects|"
    r"sends|send|finishes|finish|completes|complete|starts|ends|turns|clears|passes|fails)\b", re.I)


def condition_subject(text: str) -> str | None:
    """The noun phrase in subject position of a condition, if it is a full,
    non-time noun phrase ("invoice 7720 matches the order" → "invoice
    7720"); never anything later in the condition ("it renews this month" →
    None, not "this month")."""

    m = _COND_VERB.search(" " + text)
    if not m:
        return None
    subj = (" " + text)[: m.start()].strip(" ,")
    if not subj or is_pronoun(subj) or TIME.fullmatch(subj) or len(subj.split()) > 7:
        return None
    if re.match(r"^(?:i|we|you|he|she|they|it|there|this|that)\b", subj, re.I):
        return None
    return subj


_PREP_SPLIT = re.compile(r"\s+(?:on|in|into|to|from|for|at|with|by|onto|via)\s+", re.I)


@dataclass
class RuleSignals:
    strong_action: bool = False
    weak_action: bool = False
    frames: list[str] = field(default_factory=list)
    framed_sentences: list[tuple[str, str]] = field(default_factory=list)   # (lowercased sentence, frame)
    vague: bool = False


ACTION_V3_LEAD: dict[str, ActionKind] = {}   # filled after PHRASAL is defined (below)
_PHRASAL_HEADS: set[str] = set()              # likewise


def _lead_verb(clause: str) -> tuple[str | None, str, str]:
    low = clause.lower()
    for vague in VAGUE_VERBS:
        if low.startswith(vague + " ") or low == vague:
            return vague, "vague", clause[len(vague):].strip()
    for table, kind in ((ACTION_V3_LEAD, "action"), (INFO_V2, "info")):
        for verb in sorted(table, key=len, reverse=True):
            m = re.match(rf"{re.escape(verb)}(?:s|es|ed)?\b", low)
            if m:
                return verb, kind, clause[m.end():].strip()
    return None, "", clause


def _split(sentence: str) -> list[str]:
    verbs = sorted(list(ACTION_V2) + list(INFO_V2) + list(VAGUE_VERBS) + list(_PHRASAL_HEADS), key=len, reverse=True)
    alt = "|".join(re.escape(v) for v in verbs)
    pattern = re.compile(rf"((?:,\s*(?:and\s+)?(?:then\s+)?|\s+and\s+(?:then\s+)?|\s+then\s+)(?=(?:{alt})\b|if\b|"
                         r"once\b|when\b|unless\b)|,\s*but\s+(?!only\b))", re.I)
    parts = pattern.split(sentence)
    out: list[str] = []
    for i in range(0, len(parts), 2):
        clause = parts[i].strip(" ,")
        if clause:
            delim = parts[i - 1] if i else ""
            out.append(("then " + clause) if re.search(r"\bthen\b", delim or "", re.I) else clause)
    return out


def _kind(verb: str, rest: str) -> ActionKind:
    low = rest.lower()
    kind = ACTION_V3.get(verb, ACTION_V2.get(verb, ActionKind.OTHER))
    if verb in ("send", "transfer", "wire", "go back", "move", "put") and MONEY.search(rest):
        return ActionKind.PAYMENT
    if verb == "transfer":
        return ActionKind.PAYMENT
    if verb in ("cancel", "decline", "reject", "accept") and re.search(
            r"\b(?:meeting|appointment|reservation|booking|table|call|invitation|invite|class|session|lesson)\b", low):
        return ActionKind.BOOKING
    if verb == "approve":
        return ActionKind.INVOICE_APPROVAL if re.search(r"\b(?:invoice|bill|expense|claim|payment|purchase|request|"
                                                        r"timesheet|reimbursement)s?\b", low) else ActionKind.OTHER
    if verb == "file" and not re.search(r"\b(?:claim|form|report|return|application|expense|complaint|appeal)\b", low):
        return ActionKind.OTHER
    if verb == "place" and re.search(r"\border\b", low):
        return ActionKind.PURCHASE
    if verb == "deliver":
        return ActionKind.OTHER
    return kind


def _lemma(pp: str | None, iv: str | None, ger: str | None, obj: str) -> str | None:
    if pp:
        p = re.sub(r"\s+", " ", pp.lower())
        if p in ("gone", "off my plate"):
            return "delete"
        if p == "out of the way":
            return "pay" if re.search(r"\b(?:bill|invoice|fee|rent|payment|fine)\b", obj, re.I) else None
        if p == "stopped":
            return "cancel"
        return PARTICIPLES_V2.get(p)
    if iv:
        return _INTRANSITIVE.get(re.sub(r"\s+", " ", iv.lower()).replace("reaches", "reach").replace(
            "gets to", "get to").replace("arrives at", "arrive at").replace("goes out", "go out"))
    if ger:
        return _GERUNDS.get(ger.lower())
    return None


def _specific(target: str, kind: ActionKind, verb: str, constraints: list[str], has_evidence: bool) -> list[MissingSlot]:
    from experiments.recognizers import _specific as v1_specific

    if is_context_reference(target):
        return [MissingSlot.TARGET]
    missing = v1_specific(target, kind, verb, constraints, has_evidence)
    low = target.lower()
    if MissingSlot.TIME in missing and DATE.search(target):
        missing.remove(MissingSlot.TIME)                       # "for the 12th to the 14th", "in May"
    if MissingSlot.RECIPIENT in missing and kind is ActionKind.PAYMENT and re.search(r"\binto\s+\S+", low):
        missing.remove(MissingSlot.RECIPIENT)                  # "into the holiday fund"
    if kind is ActionKind.MESSAGE and verb in ("send", "email", "forward") and MissingSlot.RECIPIENT not in missing \
            and re.match(r"^(?:a|an)\s+(?:message|email|note|text)\b", low) and not re.search(r"\b(?:saying|that|about)\b",
                                                                                              low):
        missing.append(MissingSlot.DETAIL)       # "send a message to the landlord": which message?
    if verb == "refund" and re.search(r"\b(?:charge|payment|transaction|order|purchase|fee|bill|invoice|booking)s?\b",
                                      low) and (re.search(r"\d|(?<!^)\b[A-Z]", target) or re.search(
                                          r"\b(?:duplicate|double|extra|wrong|last|latest)\b", low)):
        # A refund of an identified charge goes back to where it came from.
        return [s for s in missing if s not in (MissingSlot.AMOUNT, MissingSlot.RECIPIENT)]
    if kind is ActionKind.PAYMENT and re.search(r"\b(?:fine|fines|toll|tolls|premium|dues|tuition|charge)\b", low) \
            and len([w for w in re.findall(r"[a-z]+", low) if w not in ("the", "my", "a", "an", "our")]) >= 2:
        # A named bill-like obligation identifies its payee ("the parking fine").
        missing = [s for s in missing if s not in (MissingSlot.AMOUNT, MissingSlot.RECIPIENT)]
    if kind is ActionKind.PAYMENT and verb in ("pay", "wire", "refund") and MissingSlot.AMOUNT not in missing:
        named = re.search(r"\b(?:bill|invoice|rent|fee|fine|subscription|tax|loan|expense|installment|instalment)s?\b", low)
        if not named and not (MONEY.search(target) and re.search(r"\d", target)):
            missing.append(MissingSlot.AMOUNT)   # "pay the contractor", "refund the customer"
    words = [w for w in re.findall(r"[a-z0-9'-]+", low) if w not in ("the", "a", "an", "my", "our", "to", "for")]
    if kind in (ActionKind.BOOKING, ActionKind.PURCHASE) and verb in ("book", "reserve", "buy", "order", "schedule") \
            and re.match(r"^(?:a|an|some|any)\b|^(?!(?:the|my|our|this|that|these|those|his|her|their)\b)[a-z]+s\b", low) \
            and len(words) <= 3 and not re.search(r"\d|[A-Z]", target):
        # "Reserve a table for Saturday", "Buy tickets for the concert", "Book a flight to Rome",
        # "Schedule the meeting": the thing is a type, not an identified item.
        if MissingSlot.TARGET not in missing:
            missing.append(MissingSlot.DETAIL)
    return list(dict.fromkeys(missing))


# ── Phase 0.4 additions ─────────────────────────────────────────────────

# B. Separable phrasal verbs: the particle may follow the object ("turn it off").
PHRASAL: dict[tuple[str, str], ActionKind] = {
    ("turn", "on"): ActionKind.SETTING_CHANGE, ("turn", "off"): ActionKind.SETTING_CHANGE,
    ("switch", "on"): ActionKind.SETTING_CHANGE, ("switch", "off"): ActionKind.SETTING_CHANGE,
    ("switch", "over"): ActionKind.SETTING_CHANGE, ("switch", "back"): ActionKind.SETTING_CHANGE,
    ("change", "back"): ActionKind.SETTING_CHANGE, ("shut", "down"): ActionKind.SETTING_CHANGE,
    ("set", "up"): ActionKind.SETTING_CHANGE, ("roll", "back"): ActionKind.SETTING_CHANGE,
    ("sign", "out"): ActionKind.SETTING_CHANGE, ("log", "out"): ActionKind.SETTING_CHANGE,
    ("move", "back"): ActionKind.OTHER, ("put", "back"): ActionKind.OTHER, ("give", "back"): ActionKind.OTHER,
    ("send", "back"): ActionKind.OTHER, ("take", "down"): ActionKind.OTHER, ("back", "up"): ActionKind.OTHER,
    ("write", "off"): ActionKind.OTHER, ("throw", "out"): ActionKind.OTHER, ("send", "out"): ActionKind.MESSAGE,
    ("top", "up"): ActionKind.PAYMENT, ("pay", "off"): ActionKind.PAYMENT, ("pay", "back"): ActionKind.PAYMENT,
    ("hand", "in"): ActionKind.FORM_SUBMISSION, ("turn", "in"): ActionKind.FORM_SUBMISSION,
    ("call", "off"): ActionKind.BOOKING, ("put", "off"): ActionKind.BOOKING, ("clear", "out"): ActionKind.FILE_DELETION,
}
ACTION_V3: dict[str, ActionKind] = {**ACTION_V2, **{f"{v} {p}": k for (v, p), k in PHRASAL.items()}}
ACTION_V3_LEAD.update(ACTION_V3)
_PHRASAL_HEADS.update(v for v, _ in PHRASAL)


def _phrasal(clause: str) -> tuple[str, str, str, str] | None:
    """"Turn the hallway heater off" → ("turn off", "action", "the hallway heater"). The words between verb and
    particle are the target; anything after the particle (a destination, a recipient) stays with it."""

    for (v, p) in sorted(PHRASAL, key=lambda vp: -len(vp[0])):
        m = re.match(rf"^{v}(?:s|es|ed)?\s+(?P<obj>(?:(?!\b{p}\b)[^,;]){{1,80}}?)\s+{p}\b(?P<rest>.*)$", clause, re.I)
        if m and not re.match(r"^(?:to|into|from|with|on|in|at|for|by)\b", m.group("obj"), re.I):
            return f"{v} {p}", "action", m.group("obj").strip(), m.group("rest").strip()
    return None


# A. Reported speech: a reporting verb WITH a complement, and not inside a condition.
REPORTED_3 = re.compile(
    r"\b(?:says|said|asks|asked|tells|told|instructs|instructed|wants|wanted|suggests|suggested|recommends|"
    r"recommended|replied|replies|wrote|writes|advised|advises|saying|keeps\s+saying|kept\s+saying)"
    r"(?:\s+(?:me|us|you|him|her|them))?(?:\s+(?:before|earlier|again|yesterday|today|last\s+week))?\s*"
    r"(?:to\b|that\b|:\s*|,?\s*QUOTE\d|(?:i|we|you)\s+(?:should|must|need\s+to|have\s+to|ought\s+to|can|could)\b)",
    re.I)


def _reported(bare: str) -> re.Match | None:
    for m in REPORTED_3.finditer(bare):
        head = bare[: m.start()]
        if re.search(r"\b(?:if|unless|once|when|whenever|after|before|until|provided|as long as|in case)\b[^,;.]*$",
                     head, re.I):
            continue    # "…only if nobody has replied by noon": a condition, not a report
        return m
    return None


# C. Possessive pronouns used as targets ("hers but not his"); "his car" is a determiner, not this.
POSSESSIVE = re.compile(r"^(mine|yours|his|hers|theirs|ours)(?=\s*(?:$|[,.;!?]|but\b|not\b|and\b|to\b|from\b|in\b|"
                        r"on\b|with\b|for\b))", re.I)
POSSESSIVE_DET = {"mine": "my", "yours": "your", "his": "his", "hers": "her", "theirs": "their", "ours": "our"}
IMPERATIVE_3 = re.compile(r"^(?P<verb>[a-z][a-z'-]+(?:\s+(?:up|out|off|on|back|down|over|in|away))?)\s+"
                          r"(?=(?:my|the|a|an|all|every|our|your|this|that|these|those|some|both|each|any|her|his|"
                          r"their|it|them|\d)\b)", re.I)

# E. Arguments by verb family, and type-specific requirements.
_PAY = {"pay", "tip", "lend", "reimburse", "settle", "pay off", "pay back"}
_MONEY_MOVE = {"transfer", "wire", "move", "put", "send", "deposit", "withdraw", "donate", "top up", "refund", "go back"}
_MESSAGE = {"send", "email", "e-mail", "forward", "share", "post", "publish", "reply", "invite", "notify", "message",
            "text", "send out", "rsvp"}
_DEST = {"move", "rename", "copy", "move back", "put back", "archive"}
_AMOUNT_RAW = (r"\d[\d,.]*\s*(?:credits?|dollars?|euros?|pounds?)\b|(?:its|their|the|my)\s+(?:remaining\s+|"
               r"whole\s+|full\s+|entire\s+)?(?:funds|balance|amount|savings)\b")
_AMOUNT = rf"(?P<amount>{_AMOUNT_RAW})"


def parse_arguments(verb: str, text: str, kind_hint: ActionKind | None, *, extra: str = "") -> dict:
    """`text` holds the object; `extra` is what followed it (a phrasal particle's remainder, an outcome's tail)."""

    t = (text or "").strip(" ,.")
    extra = (extra or "").strip(" ,.")
    content = re.search(r"\s+(?P<c>(?:saying|that|about|telling|asking|to say)\b.*)$", t, re.I)
    content_clause = content.group("c") if content and verb in _MESSAGE else None
    if content_clause:
        t = t[: content.start()].strip()
    if extra and not re.search(r"\b(?:to|into|with|on|from)\b", t, re.I):
        joined = f"{t} {extra}"
    else:
        joined = t
    t = re.sub(r"\s+(?:to|for)\s+(?:me|us)\s*$", "", t, flags=re.I) if verb not in _MESSAGE else t
    out: dict = {"object": t, "amount": None, "payee": None, "obligation": None, "recipient": None,
                 "destination": None, "refers_back": bool(re.search(r"\b(?:its|their)\b", t, re.I)),
                 "content_clause": content_clause}
    if extra:
        d = re.match(r"^(?:back\s+)?(?:to|into)\s+(?P<d>.+)$", extra, re.I)
        if d and verb not in _MESSAGE and verb not in _MONEY_MOVE:
            out["destination"] = d.group("d")
    t_full = joined
    money = bool(MONEY.search(t)) or bool(re.search(_AMOUNT, t, re.I))
    if verb in _PAY:
        m = re.match(rf"^(?P<payee>(?!{_AMOUNT_RAW}).+?)\s+{_AMOUNT}", t, re.I)
        if NAMED_OBLIGATION.search(t):
            out["obligation"] = t
        if m:
            out.update(payee=m.group("payee"), amount=m.group("amount"), object=m.group("payee"))
        else:
            a = re.search(_AMOUNT, t, re.I)
            p = re.search(r"\b(?:to|into)\s+(?P<p>.+)$", t, re.I)
            out.update(amount=a.group("amount") if a else None, payee=p.group("p") if p else None)
            if out["obligation"] is None and p is None and not a:
                out["payee"] = t               # "pay the electrician": a payee, no amount
        return out
    if verb in _MONEY_MOVE and (money or kind_hint is ActionKind.PAYMENT or verb in ("transfer", "wire", "donate",
                                                                                  "withdraw", "top up", "refund")):
        if verb == "refund":
            out["obligation"] = t
            return out
        a = re.search(_AMOUNT, t, re.I)
        out["amount"] = a.group("amount") if a else None
        m = re.match(rf"^(?P<payee>.+?)\s+with\s+{_AMOUNT}", t, re.I) if verb == "top up" else None
        if m:
            out.update(payee=m.group("payee"), amount=m.group("amount"))
            return out
        p = re.search(r"\b(?:to|into)\s+(?P<p>[^,;]+)$", t_full, re.I) or (
            re.search(r"\bfrom\s+(?P<p>[^,;]+?)(?:\s+(?:into|to)\b|$)", t_full, re.I) if verb == "withdraw" else None)
        out["payee"] = p.group("p") if p else None
        return out
    if verb in _MESSAGE:
        if verb == "reply":
            r = re.match(r"^to\s+(?P<r>.+?)(?:\s+(?:that|saying|with)\b.*)?$", t, re.I)
            out.update(recipient=r.group("r") if r else None, object=t)
            return out
        io = re.match(r"^(?P<r>(?:[A-Z][\w'-]+(?:\s+[A-Z][\w'-]+)*|him|her|them|me|us|my\s+\w+))\s+(?P<o>(?:the|my|our|"
                      r"a|an|this|that|these|those|his|her|their|its)\b.+)$", t)
        if io:
            out.update(recipient=io.group("r"), object=io.group("o"))
            return out
        for prep in (r"\s+(?:out\s+)?to\s+", r"\s+with\s+", r"\s+on\s+"):
            parts = re.split(prep, t_full, maxsplit=1, flags=re.I)
            if len(parts) == 2 and parts[0]:
                key = "recipient" if "on" not in prep else "destination"
                out.update(object=parts[0], **{key: parts[1]})
                return out
        return out
    if verb in _DEST:
        m = re.match(r"^(?P<o>.+?)\s+(?:back\s+)?(?:to|into)\s+(?P<d>.+)$", t, re.I)
        if m:
            out.update(object=m.group("o"), destination=m.group("d"))
    return out


def required_missing(kind: ActionKind, verb: str, args: dict, *, target_ok: bool, time_text: str) -> list[MissingSlot]:
    """Only what identifies the operation and its target, by type (completeness.py's table)."""

    missing: list[MissingSlot] = []
    if kind is ActionKind.PAYMENT:
        if verb == "refund" or (args.get("obligation") and verb in _PAY | {"pay"}):
            if not target_ok:
                missing.append(MissingSlot.TARGET)
            return missing
        if not args.get("amount"):
            missing.append(MissingSlot.AMOUNT)
        payee = args.get("payee")
        if not payee or not identifiable_recipient(payee).ok and not re.search(r"\b(?:savings|checking|account|card|"
                                                                               r"fund|wallet|phone|pot)\b", payee, re.I):
            missing.append(MissingSlot.RECIPIENT)
        return missing
    if kind is ActionKind.MESSAGE:
        if not target_ok and verb != "reply":
            missing.append(MissingSlot.TARGET)
        if verb in ("post", "publish"):
            return missing
        rec = args.get("recipient")
        if (verb == "reply" and not rec and target_ok) or args.get("implicit_recipient"):
            return missing
        if not rec or not identifiable_recipient(rec).ok:
            missing.append(MissingSlot.RECIPIENT)
        return missing
    if not target_ok:
        missing.append(MissingSlot.TARGET)
    if verb in ("move", "rename", "copy", "move back", "put back") and not args.get("destination"):
        missing.append(MissingSlot.DETAIL)
    if kind is ActionKind.BOOKING and verb in ("book", "reserve", "schedule", "rebook", "reschedule") and not (
            TIME.search(time_text) or DATE.search(time_text) or EVENT_TIME.search(time_text)):
        missing.append(MissingSlot.TIME)
    return missing


def recognize_rules_v3(prompt: str, evidence: Sequence[EvidenceItem] = ()) -> tuple[IntentResult, RuleSignals]:
    stripped, quotes = _strip_quotes(prompt)
    signals = RuleSignals()
    sigs: list[SuspicionSignal] = []
    reasons: list[SuspicionReason] = []
    quoted = [QuotedInstruction(text=q, source="prompt_quote") for q in quotes
              if any(re.search(rf"\b{re.escape(v)}\b", q.lower()) for v in ACTION_V2)]
    quoted += _evidence_instructions(evidence)
    sigs += [SuspicionSignal(span=q.text[:120], origin="evidence" if q.source == "evidence" else "quoted",
                             kind="explicit") for q in quoted]
    has_evidence = bool(evidence)
    evidence_ids = tuple(e.id for e in evidence)
    candidates: list[ActionCandidate] = []
    subtasks: list[Subtask] = []
    missing: list[Missing] = []
    uncertainties: list[str] = []
    disc = Discourse()
    clause_no = 0
    earlier: list[tuple[str, str]] = []          # (step id, clause) for condition recovery
    steps: list[tuple[str, str]] = []            # (step id, verb) of informational steps, for their products

    def framed(sentence: str, frame: str) -> None:
        signals.frames.append(frame)
        signals.framed_sentences.append((sentence.lower(), frame))
        if any(re.search(rf"\b{re.escape(v)}\b", sentence.lower()) for v in ACTION_V2) or re.search(
                rf"\b(?:{_PP})\b", sentence, re.I):
            origin = {"fiction": "fiction", "hypothetical": "hypothetical", "advice": "advice",
                      "reported": "reported"}[frame]
            sigs.append(SuspicionSignal(span=sentence[:120], origin=origin, kind="explicit"))

    def restore(text: str) -> str:
        """Put quoted names back into a target ("Unpin the "Q3 planning" channel"): a name the user wrote is
        part of their request, not an instruction. Quoted *instructions* are still found at sentence level."""

        return re.sub(r"QUOTE(\d+)", lambda m: f"\"{quotes[int(m.group(1)) - 1]}\"", text or "").strip()

    def add_candidate(verb: str, rest: str, *, kind_signal: str, conds: list[Condition], approval: bool,
                      sequential: bool, objective: str, in_chain: bool, clause_index: int, explicit_obj: str | None = None,
                      kind_hint: ActionKind | None = None, slot_extra: str = "") -> None:
        text = restore(explicit_obj if explicit_obj is not None else rest)
        slot_extra = restore(slot_extra)
        target_raw, constraints = _target_and_constraints(text)
        # A preposition left dangling once a trailing time is cut ("it for next Friday" → "it for") is not target.
        target_raw = re.sub(r"\s+(?:for|on|at|by|in|to|from|with|before|until|after)$", "", target_raw, flags=re.I)
        constraints += [m.group(0) for m in TIME.finditer(slot_extra)]
        args = parse_arguments(verb, target_raw, kind_hint, extra=slot_extra)
        obj = args["object"]
        approval = approval or any(c.kind == "approval" for c in conds)
        conds = [c for c in conds if c.kind != "approval"]
        cond_np = next((condition_subject(c.text) for c in conds if condition_subject(c.text)), None)
        core = obj
        if obj and re.match(r"^(?:it|them|those|these|that|this|him|her)\b", obj, re.I):
            core = _PREP_SPLIT.split(obj, maxsplit=1)[0]
        possessive = POSSESSIVE.match(core or "")
        pronoun = is_pronoun(core) or bool(possessive)
        depends: list[str] = []
        checks: list[str] = []
        alternatives: tuple[str, ...] = ()
        tkind, source, method, resolved_text = TargetKind.NONE, "request", "explicit", None
        if possessive:
            det = POSSESSIVE_DET[possessive.group(1).lower()]
            owned = [e for e in disc.entities if e.clause < clause_index and re.match(rf"^{det}\b", e.text, re.I)]
            if len(owned) == 1:
                tkind, method, resolved_text, source = TargetKind.PREVIOUS_CLAUSE, "possessive", owned[0].text, \
                    f"step:{owned[0].source}"
                if owned[0].source[:1] in "sc":
                    depends.append(owned[0].source)
            elif len(owned) > 1:
                tkind, method, alternatives = TargetKind.MULTIPLE, "possessive", tuple(e.text for e in owned)
                reasons.append(SuspicionReason.MULTIPLE_ANTECEDENTS)
            else:
                method = "possessive"
                reasons.append(SuspicionReason.UNRESOLVED_PRONOUN)
        elif pronoun or is_context_reference(core):
            res = disc.resolve(core, clause=clause_index, in_chain=in_chain, condition_subject=cond_np,
                               evidence_ids=evidence_ids, refers_to_evidence=bool(EVIDENCE_REF.search(obj or "")))
            method = {"unique": "unique_antecedent", "chain": "chain", "condition_subject": "condition_subject",
                      "evidence": "evidence", "multiple": "unique_antecedent", "no_antecedent": "unique_antecedent",
                      "context_reference": "context_reference"}.get(res.status, res.status)
            if res.status in ("unique", "chain"):
                tkind, resolved_text, source = TargetKind.PREVIOUS_CLAUSE, res.text, f"step:{res.provenance}"
                if res.provenance and res.provenance[0] in "sc" and res.provenance[1:].isdigit():
                    depends.append(res.provenance)
            elif res.status == "condition_subject":
                tkind, resolved_text, source = TargetKind.CONTEXT_UNIQUE, res.text, "condition"
            elif res.status == "multiple":
                tkind, alternatives = TargetKind.MULTIPLE, tuple(res.candidates)
                reasons.append(SuspicionReason.MULTIPLE_ANTECEDENTS)
            else:
                reasons.append(SuspicionReason.UNRESOLVED_TARGET if res.status == "context_reference"
                               else SuspicionReason.UNRESOLVED_PRONOUN)
        else:
            idf = identifiable(obj, evidence=evidence, steps=steps, kind=kind_hint or _kind(verb, obj or ""))
            if not idf.ok and args.get("content_clause") and re.match(
                    r"^(?:a|an)\s+(?:email|message|note|text|reminder)\b", obj or "", re.I):
                idf = type(idf)(True, "a message whose content is given")      # "an email … saying X"
            checks.append(f"identifiable:{'pass' if idf.ok else 'fail'}:{idf.reason}")
            if idf.ok:
                tkind = (TargetKind.CONTEXT_UNIQUE if idf.source.startswith("evidence")
                         else TargetKind.PREVIOUS_CLAUSE if idf.source.startswith("step") else TargetKind.EXACT)
                resolved_text, source = obj, idf.source
                if idf.source.startswith("step"):
                    depends.append(idf.source.split(":", 1)[1])
            elif obj:
                tkind = TargetKind.GENERIC
                reasons.append(SuspicionReason.UNRESOLVED_TARGET)
        kind = kind_hint or _kind(verb, f"{resolved_text or obj or ''} {target_raw} {slot_extra}")
        if resolved_text is not None:
            via = "explicit" if tkind is TargetKind.EXACT else method
            checked, sem = semantic_checks(resolved_text, pronoun=core if pronoun else None, kind=kind, via=via)
            checks += [f"{c.name}:{'pass' if c.passed else 'fail'}:{c.detail}" for c in sem]
            if checked is None:
                tkind, resolved_text = TargetKind.NONE, None
                reasons.append(SuspicionReason.UNRESOLVED_TARGET)
            else:
                resolved_text = checked
        if subtasks and not depends:
            depends.append(subtasks[-1].subtask_id)
        if sequential and candidates:
            depends.append(candidates[-1].candidate_id)
        if args.get("refers_back") and candidates and candidates[-1].candidate_id not in depends:
            depends.append(candidates[-1].candidate_id)
        grounded = grounded_span(resolved_text, prompt) if resolved_text else None
        if resolved_text and not grounded:
            checks.append("grounded:fail:the resolved text is not a span of the request")
            tkind, resolved_text = TargetKind.NONE, None
        if resolved_text and NAMED_OBLIGATION.search(resolved_text) and tkind in RESOLVED_KINDS:
            args["obligation"] = resolved_text          # "pay it" → "the tax bill of 1,200 credits"
        if resolved_text and re.search(r"\b(?:reply|response|answer)\b", resolved_text, re.I):
            args["implicit_recipient"] = True           # a reply goes to whoever wrote the message
        slots = required_missing(kind, verb, args, target_ok=tkind in RESOLVED_KINDS,
                                 time_text=f"{target_raw} {slot_extra} {' '.join(constraints)} {objective}")
        complete = all(c.complete for c in conds) and not (any(c.pronoun_subject for c in conds)
                                                           and tkind not in RESOLVED_KINDS)
        if not complete:
            reasons.append(SuspicionReason.INCOMPLETE_CONDITION)
            if not all(c.complete for c in conds):
                slots.append(MissingSlot.CONDITION)
        if any(c.ambiguous_approval for c in conds):
            reasons.append(SuspicionReason.AMBIGUOUS_APPROVAL)
        evidence_refs = evidence_ids if (EVIDENCE_REF.search(obj or "") and has_evidence) else ()
        if source.startswith("evidence:"):
            evidence_refs = (source.split(":", 1)[1],)
        cid = f"c{len(candidates) + 1}"
        resolution = TargetResolution(
            original=grounded_span(core or obj, prompt) or (core or obj or None), resolved=grounded,
            kind=tkind if grounded or tkind not in RESOLVED_KINDS else TargetKind.NONE, source=source, method=method,
            alternatives=alternatives if tkind is TargetKind.MULTIPLE else (),
            disambiguation_needed=tkind is TargetKind.MULTIPLE, checks=tuple(checks))
        display = grounded
        if kind is ActionKind.MESSAGE and grounded and not pronoun and tkind in RESOLVED_KINDS:
            display = grounded_span(target_raw, prompt) or grounded      # content and recipient, as written
        cond_texts = tuple(grounded_span(c.text, prompt) or c.text for c in conds)
        slots = list(dict.fromkeys(slots))
        specified = not slots and bool(grounded)
        candidates.append(ActionCandidate(
            candidate_id=cid, action_type=kind, verb=verb, objective=objective[:200], target=display,
            target_specified=specified, constraints=tuple(constraints), conditions=cond_texts,
            conditional=bool(cond_texts), requires_user_approval=approval, depends_on=tuple(dict.fromkeys(depends)),
            evidence_refs=evidence_refs, needs_clarification=not specified or not complete,
            uncertainties=() if kind_signal == "explicit" else (f"{kind_signal} request",),
            target_provenance=resolution.kind.value, pronoun_target=pronoun,
            condition_kinds=tuple(c.kind for c in conds), conditions_complete=complete,
            target_resolution=resolution, missing_slots=tuple(s.value for s in slots),
        ))
        missing.extend(Missing(candidate_id=cid, slot=s) for s in slots)
        if not specified and not slots:
            missing.append(Missing(candidate_id=cid, slot=MissingSlot.TARGET))
        sigs.append(SuspicionSignal(span=objective[:120], origin="user_instruction", kind=kind_signal))  # type: ignore[arg-type]
        if grounded:
            disc.add(grounded, cid, clause_index)
        if kind_signal == "explicit":
            signals.strong_action = True
        else:
            signals.weak_action = True

    pending: Condition | None = None
    for sentence in _sentences(stripped):
        bare = sentence.strip()
        low = bare.lower()
        if FICTION.search(bare):
            framed(bare, "fiction")
            continue
        if HYPOTHETICAL.search(bare) or HYPOTHETICAL_2.search(bare):
            framed(bare, "hypothetical")
            continue
        pm = STATUS_CLAIM_REQUEST.match(bare)
        if pm and PARTICIPLES_V2.get(pm.group("pp").lower()):
            add_candidate(PARTICIPLES_V2[pm.group("pp").lower()], pm.group("obj"), kind_signal="indirect", conds=[],
                          approval=False, sequential=False, objective=bare, in_chain=False, clause_index=clause_no)
            clause_no += 1
            uncertainties.append("asks to be told an action happened")
            continue
        polite = POLITE_REQUEST.match(bare)
        if bare.endswith("?") and not polite and QUESTION_LEAD.match(bare):
            question_request = any(p.search(bare.rstrip("?")) for p, _ in OUTCOME_PATTERNS[-1:])
            if not question_request:
                framed(bare, "advice")
                continue
        if ADVICE_LEAD.match(bare) and re.search(r"\bhow\b", low):
            framed(bare, "advice")
            continue
        reported = _reported(bare)
        if reported:
            parts = re.split(r"(,?\s+so\s+|;\s*|,?\s+but\s+)", bare, maxsplit=1)
            head, tail = (parts[0], parts[2]) if len(parts) == 3 else (bare, "")
            quoted.append(QuotedInstruction(text=head[:200], source="reported"))
            framed(head, "reported")
            hm = re.search(r"\bto\s+(.+)$", head[reported.start():])
            if hm:
                v, k, rest = _lead_verb(hm.group(1))
                if k == "action":
                    disc.add(grounded_span(_target_and_constraints(rest)[0], prompt), "reported", clause_no)
            clause_no += 1
            if not tail:
                continue
            bare = tail
        approval = bool(APPROVAL_2.search(bare))
        bare = APPROVAL_2.sub("", bare).strip(" ,")
        bare = ASAP.sub("", bare).strip(" ,")              # "as soon as possible" is a time, not a condition
        clauses = _split(bare.rstrip(".?!"))
        sentence_condition: Condition | None = None        # a leading condition covers every action of its sentence
        for c_index, raw in enumerate(clauses):
            ci = clause_no
            clause_no += 1
            clause = raw.strip()
            sequential = bool(re.match(r"^(?:and\s+)?then\b", clause, re.I))
            m = CONDITION_LEAD_2.match(clause)
            if m and "," not in m.group("cond"):
                # A condition's noun phrases are never discourse antecedents for
                # targets; only its subject may be used, explicitly (condition_subject).
                pending = analyze_condition(m.group("cond").strip(" ,"), m.group("conn"), earlier_clauses=earlier)
                continue
            conds: list[Condition] = []
            if m:
                cond, _, main = m.group("cond").partition(",")
                conds.append(analyze_condition(cond.strip(), m.group("conn"), earlier_clauses=earlier))
                clause = main.strip()
                if c_index == 0:
                    sentence_condition = conds[-1]
            if pending is not None:
                conds.append(pending)
                if c_index <= 1:
                    sentence_condition = pending
                pending = None
            elif sentence_condition is not None and c_index > 0 and sentence_condition not in conds:
                conds.append(sentence_condition)
            while True:
                new = LEAD_STRIP.sub("", clause)
                if new == clause:
                    break
                clause = new
            # Outcome requests first: their "if" ("I'd appreciate it if…") is not a condition.
            hit = None
            for pattern, sig_kind in OUTCOME_PATTERNS:
                om = pattern.search(clause)
                if om:
                    hit = (om, sig_kind)
                    break
            if hit:
                om, sig_kind = hit
                gd = om.groupdict()
                obj = gd.get("obj") or ""
                lemma = _lemma(gd.get("pp") or gd.get("pp2"), gd.get("iv"), gd.get("ger"), obj)
                if gd.get("rec") and not lemma:
                    lemma = "send"
                tail = clause[om.end():]
                t = CONDITION_TRAIL_2.search(" " + tail) if not WISH_FRAME.match(clause) else None
                if t:
                    conds.append(analyze_condition(t.group("cond").strip(" ,."), t.group("conn"),
                                                   earlier_clauses=earlier))
                if lemma:
                    kind_hint = ActionKind.MESSAGE if gd.get("rec") else None
                    add_candidate(lemma, obj, kind_signal=sig_kind, conds=conds, approval=approval,
                                  sequential=sequential, objective=clause, in_chain=c_index > 0, clause_index=ci,
                                  explicit_obj=obj, kind_hint=kind_hint or _kind(lemma, obj + " " + tail),
                                  slot_extra=(f"to {gd['rec']} " if gd.get("rec") else "") + tail)
                else:
                    sigs.append(SuspicionSignal(span=clause[:120], origin="user_instruction", kind="outcome"))
                    reasons.append(SuspicionReason.OUTCOME_REQUESTED)
                    missing.append(Missing(candidate_id=None, slot=MissingSlot.ACTION))
                earlier.append((f"k{ci}", clause))
                continue
            if VAGUE_OUTCOME.search(clause):
                sigs.append(SuspicionSignal(span=clause[:120], origin="user_instruction", kind="outcome"))
                reasons.append(SuspicionReason.OUTCOME_REQUESTED)
                missing.append(Missing(candidate_id=None, slot=MissingSlot.ACTION))
                uncertainties.append("an outcome is wanted but not which action")
                continue
            pv = _phrasal(clause)
            phrasal_rest = ""
            if pv:
                verb, kind, rest, phrasal_rest = pv
            else:
                verb, kind, rest = _lead_verb(clause)
            if kind == "info":
                sid = f"s{len(subtasks) + 1}"
                subtasks.append(Subtask(subtask_id=sid, kind=INFO_V2[verb] if INFO_V2[verb] in Subtask.model_fields[
                    "kind"].annotation.__args__ else "other", text=clause.rstrip(" ,and")))  # type: ignore[union-attr]
                obj = _target_and_constraints(rest)[0]
                obj = re.sub(r"\s+(?:to|for|with)\s+(?:me|us)\b.*$", "", obj, flags=re.I).strip()
                steps.append((sid, verb))
                np_ = first_np(obj) if obj else None
                if verb in ("compare", "match", "contrast") and obj:
                    # "compare my draft with your draft": two antecedents.
                    obj = re.sub(r"\s+(?:with|against|to|and)\s+", " and ", obj, count=1, flags=re.I)
                cand_obj = obj if obj and not re.match(r"^(?:when|whether|if|what|how|which)\b", obj, re.I) else np_
                if cand_obj and not is_pronoun(cand_obj):
                    # A coordinated object ("the oak desk and the pine desk") is two antecedents, not one.
                    parts = [x.strip() for x in re.split(r"\s*,\s*(?:and\s+|or\s+)?|\s+(?:and|or)\s+", cand_obj)
                             if x.strip()]
                    if len(parts) > 1 and all(re.match(rf"^{_NP_START}", x, re.I) for x in parts):
                        for x in parts:
                            disc.add(grounded_span(x, prompt), sid, ci)
                    else:
                        disc.add(grounded_span(cand_obj, prompt), sid, ci)
                earlier.append((sid, clause))
                continue
            if kind == "vague":
                signals.vague = True
                missing.append(Missing(candidate_id=None, slot=MissingSlot.ACTION))
                uncertainties.append(f"'{verb}' does not say which action is wanted")
                sigs.append(SuspicionSignal(span=clause[:120], origin="user_instruction", kind="indirect"))
                continue
            if kind == "action":
                t = CONDITION_TRAIL_2.search(" " + rest)
                if t:
                    conds.append(analyze_condition(t.group("cond").strip(" ,."), t.group("conn"),
                                                   earlier_clauses=earlier))
                    rest = (" " + rest)[: t.start()].strip(" ,")
                add_candidate(verb, rest, kind_signal="explicit", conds=conds, approval=approval, sequential=sequential,
                              objective=clause, in_chain=c_index > 0, clause_index=ci, slot_extra=phrasal_rest)
                earlier.append((candidates[-1].candidate_id, clause))
                continue
            im = IMPERATIVE_3.match(clause)
            if im and "'" not in im.group("verb") and im.group("verb").lower().split()[0] not in FUNCTION_WORDS \
                    and im.group("verb").lower() not in TEXT_ONLY:
                sigs.append(SuspicionSignal(span=clause[:120], origin="user_instruction", kind="unknown_verb"))
                reasons.append(SuspicionReason.IMPERATIVE_ACTION_UNRESOLVED)
                missing.append(Missing(candidate_id=None, slot=MissingSlot.DETAIL))
                uncertainties.append(f"'{im.group('verb')}' reads as a request but is not a known operation")

    executable = [c for c in candidates if c.mode is RequestMode.EXECUTE]
    user_sig = any(s.origin == "user_instruction" for s in sigs)
    if user_sig and not executable:
        reasons.append(SuspicionReason.POSSIBLE_SIDE_EFFECT_WITHOUT_CANDIDATE)
    suspicion = ActionSuspicion(
        signals=tuple(sigs), candidate_extracted=bool(executable),
        target_resolved=(all(c.target_specified for c in executable) if executable else None),
        condition_complete=(all(c.conditions_complete for c in executable if c.conditional or not c.conditions_complete)
                            if any(c.conditional or not c.conditions_complete for c in executable) else None),
        reasons=tuple(dict.fromkeys(reasons)))
    if executable:
        cls = IntentClass.ACTION
    elif user_sig or signals.vague or missing:
        cls = IntentClass.AMBIGUOUS
        if not (missing or uncertainties):
            uncertainties.append("possible side effect without a candidate")
    else:
        cls = IntentClass.INFORMATIONAL
    if cls is IntentClass.AMBIGUOUS and not missing:
        missing.append(Missing(candidate_id=None, slot=MissingSlot.DETAIL))
    return IntentResult(classification=cls, candidates=tuple(candidates), subtasks=tuple(subtasks),
                        quoted_instructions=tuple(quoted), missing=tuple(missing),
                        uncertainties=tuple(dict.fromkeys(uncertainties)), source="deterministic",
                        suspicion=suspicion), signals


# General informational verbs v2 lacked ("look at", "go over", "read through").
INFO_V2.update({"look at": "review", "go over": "review", "read through": "review", "skim": "review",
                "check over": "review", "look over": "review"})

_FRAMES_KEPT = {"advice", "hypothetical", "fiction", "reported"}


def recognize_hybrid_v3(rules: IntentResult, signals: RuleSignals, model: IntentResult | None,
                        prompt: str) -> IntentResult:
    """As the v1 hybrid, except: a model action that lies inside a sentence the
    rules framed (advice, hypothetical, fiction, reported) or inside a quote
    keeps the frame. Disagreement elsewhere still clarifies. The model never
    removes caution."""

    from experiments.recognizers import recognize_hybrid as v1

    if model is None:
        return v1(rules, signals, model, prompt)
    if rules.classification is IntentClass.INFORMATIONAL and model.executable:
        inside = []
        for c in model.executable:
            obj = (c.objective or "").lower()
            in_frame = any(obj and obj in s for s, f in signals.framed_sentences if f in _FRAMES_KEPT)
            in_quote = any(obj and obj in q.text.lower() for q in rules.quoted_instructions)
            inside.append(in_frame or in_quote)
        if all(inside):
            return rules.model_copy(update={
                "source": "hybrid", "rejected": model.rejected,
                "uncertainties": rules.uncertainties + ("model read an action inside a framed or quoted sentence; "
                                                        "the frame was kept",)})
        susp = rules.suspicion or ActionSuspicion()
        merged = susp.model_copy(update={"reasons": susp.reasons + (SuspicionReason.RECOGNIZER_DISAGREEMENT,)})
        out = v1(rules, signals, model, prompt)
        return out.model_copy(update={"suspicion": merged})
    if rules.classification is IntentClass.ACTION:
        return _merge_action(rules, model)
    out = v1(rules, signals, model, prompt)
    return out.model_copy(update={"suspicion": rules.suspicion})


def _merge_action(rules: IntentResult, model: IntentResult) -> IntentResult:
    """v1's merge for an action the rules found, with one correction made after
    the freeze (docs/31 §11.7): a condition the model adds is analysed like
    any other (kind, completeness), so an incomplete one clarifies. (v1's
    merge appended it with no kind, which the v2 contract rejects.)"""

    merged = list(rules.candidates)
    missing = list(rules.missing)
    notes: list[str] = []
    model_exec = list(model.executable)
    for i, c in enumerate(merged):
        twin = next((m for m in model_exec if m.action_type is c.action_type), None)
        if twin is None:
            continue
        extra = [analyze_condition(x, "if") for x in twin.conditions if x not in c.conditions]
        approval = c.requires_user_approval or twin.requires_user_approval
        if extra or approval != c.requires_user_approval:
            complete = c.conditions_complete and all(x.complete for x in extra)
            merged[i] = c.model_copy(update={
                "conditions": c.conditions + tuple(x.text for x in extra), "conditional": True if extra else c.conditional,
                "condition_kinds": c.condition_kinds + tuple(x.kind for x in extra), "conditions_complete": complete,
                "needs_clarification": c.needs_clarification or not complete, "requires_user_approval": approval})
            if not complete:
                missing.append(Missing(candidate_id=c.candidate_id, slot=MissingSlot.CONDITION))
    for m in model_exec:
        if not any(c.action_type is m.action_type for c in rules.executable):
            cid = f"c{len(merged) + 1}"
            merged.append(m.model_copy(update={
                "candidate_id": cid, "depends_on": (), "needs_clarification": True, "target_specified": False,
                "uncertainties": ("recognized only by the model",),
                "condition_kinds": tuple(analyze_condition(x, "if").kind for x in m.conditions)}))
            missing.append(Missing(candidate_id=cid, slot=MissingSlot.DETAIL))
            notes.append(f"model-only {m.action_type.value} candidate held for clarification")
    return IntentResult(classification=IntentClass.ACTION, candidates=tuple(merged), subtasks=rules.subtasks,
                        quoted_instructions=rules.quoted_instructions + model.quoted_instructions,
                        missing=tuple(missing), uncertainties=rules.uncertainties + tuple(notes), source="hybrid",
                        rejected=model.rejected, suspicion=rules.suspicion)


__all__ = ["RULES_VERSION", "RuleSignals", "parse_arguments", "recognize_hybrid_v3", "recognize_rules_v3",
           "required_missing"]

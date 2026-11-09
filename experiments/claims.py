"""Finding statements in model text that assert an action happened (Phase
0.1, docs/31 §9). **Defence in depth, not the boundary.**

The boundary is structural (`finalizer.py`): the user is told an action's
status only by the harness, from the `ActionLedger`; the model has no field
and no channel that sets or states a status. This module screens the model's
free text for sentences that would *contradict* that by asserting a side
effect on their own, so they can be withheld. It is lexical and therefore
incomplete by construction: a sentence it misses still cannot change what the
harness says about the action, and on an action-intent task one hit withholds
the model's whole contribution (fail closed).

Two policies:
* ``action_intent`` (the task has a requested action in the ledger): strict.
  Any non-negated, non-conditional completion form of a side-effect verb, any
  completion idiom, any implied end state ("your folder is now empty", "check
  your balance"), any receipt or identifier the context did not supply.
* ``informational`` (no action requested): only claims with agency, the
  assistant's own ("I've sent…") or on the user's behalf ("your files have
  been deleted"), strong completion idioms ("it's done") and receipts. A
  third-party event from the evidence ("the board approved v3.1") is not a
  claim that the assistant did something.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

ClaimKind = Literal["action_completion", "goal_achievement", "fabricated_evidence", "status_assertion"]
Policy = Literal["action_intent", "informational"]

_PAST = (
    r"transferred|paid|deleted|removed|erased|wiped|approved|sent|submitted|booked|purchased|bought|ordered|"
    r"cancell?ed|changed|updated|scheduled|processed|executed|initiated|completed|moved|archived|posted|"
    r"published|forwarded|refunded|charged|wired|deposited|withdrawn|withdrew|signed|accepted|confirmed|"
    r"reserved|installed|uninstalled|enabled|disabled|reset|unsubscribed|filed|marked|cleared|emptied|settled|"
    r"authori[sz]ed|shortened|edited|rewritten|rewrote|created|saved|uploaded|shared|emailed|messaged|replied|"
    r"notified|handled|finished|done|actioned|carried\s+out|taken\s+care\s+of|set\s+up|applied|issued|released|"
    r"granted|revoked|closed|renewed|placed|dispatched|transmitted|delivered|fulfilled|resolved|wrapped\s+up"
)
_PROG = (
    r"transferring|paying|deleting|removing|erasing|wiping|approving|sending|submitting|booking|purchasing|"
    r"buying|ordering|cancell?ing|changing|updating|scheduling|processing|executing|initiating|completing|"
    r"moving|archiving|posting|publishing|forwarding|refunding|charging|wiring|depositing|withdrawing|signing|"
    r"accepting|confirming|reserving|installing|enabling|disabling|resetting|clearing|emptying|settling|"
    r"authori[sz]ing|handling|finali[sz]ing"
)
_BASE = (
    r"transfer|pay|delete|remove|erase|wipe|approve|send|submit|book|purchase|buy|order|cancel|change|update|"
    r"schedule|process|execute|initiate|complete|move|archive|post|publish|forward|refund|charge|wire|deposit|"
    r"withdraw|sign|accept|confirm|reserve|install|enable|disable|reset|clear|empty|settle|authori[sz]e|handle|"
    r"take\s+care\s+of|finali[sz]e|get\s+(?:this|that|it)\s+done|do\s+(?:this|that|it)"
)

_FLAGS = re.IGNORECASE

# "I've deleted", "we have now sent", "I went ahead and paid", "I'm processing",
# "I'll transfer it now". A negation between subject and verb ("I have not
# deleted") breaks the chain, so it does not match.
_AUX1 = (r"(?:\s*'(?:ve|ll|m|d|re)|\s+(?:have|has|had|will|shall|am|are|was|were|just|now|already|successfully|"
         r"also|then|been|gone\s+ahead\s+and|went\s+ahead\s+and|proceeded\s+to|going\s+to|go\s+ahead\s+and|"
         r"can\s+confirm\s+(?:that\s+)?(?:i|we)(?:'ve|\s+have)?))")
FIRST_PERSON = re.compile(
    rf"\b(?:i|we)\b(?P<aux>{_AUX1}*)\s+(?:(?P<past>{_PAST})|(?P<prog>{_PROG})|(?P<base>{_BASE}))\b", _FLAGS)
_FUTURE_AUX = re.compile(r"'ll|will|shall|going\s+to|go(?:ne)?\s+ahead\s+and|went\s+ahead\s+and|proceeded\s+to",
                         _FLAGS)

# "has been approved", "is being initiated", "were deleted", "got sent".
# "has not been approved" / "wasn't deleted" do not match.
PASSIVE = re.compile(
    r"\b(?:(?:has|have|had)\s+(?:now\s+|already\s+|just\s+|successfully\s+|also\s+)?been|is\s+being|are\s+being|"
    r"'s\s+been|'ve\s+been|was|were|is|are|'s|'re|got|gets|will\s+be|has\s+gone|have\s+gone)\s+"
    rf"(?:now\s+|already\s+|successfully\s+|fully\s+|all\s+|just\s+|also\s+|been\s+)?(?:{_PAST})\b",
    _FLAGS,
)
# Who it was done for. A passive without one of these, on an informational
# task, describes the world (often the evidence), not the assistant's act.
AGENCY = re.compile(r"\b(?:your|yours|you'?re|for\s+you|on\s+your\s+behalf|as\s+(?:you\s+)?(?:requested|asked|"
                    r"instructed)|i|we|me|us|my|our)\b", _FLAGS)

# Completion idioms that need no verb from the lists above.
STRONG_IDIOMS = re.compile(
    r"\b(?:it'?s|it\s+is|that'?s|that\s+is|this\s+is|all|everything(?:'s|\s+is|\s+has\s+been)?)\s+(?:now\s+|all\s+)?"
    r"(?:done|set|sorted|handled|complete|completed|finished|taken\s+care\s+of)\b"
    r"|\b(?:i'?ve|i\s+have|we'?ve|we\s+have)\s+(?:now\s+|already\s+)?(?:taken\s+care\s+of|handled|sorted|done)\b"
    r"|\btaken\s+care\s+of\b|\bconsider\s+it\s+(?:done|handled|sorted|taken\s+care\s+of)\b|^\W*(?:done|all\s+done)\b"
    r"|\bmission\s+accomplished\b|\byou'?re\s+(?:all\s+)?(?:set|good\s+to\s+go)\b"
    r"|\bas\s+requested\b.*\b(?:" + _PAST + r")\b",
    _FLAGS | re.MULTILINE,
)
# Completion words that also describe the world or a reading ("the backup
# completed successfully", "I went through the documents"): a claim on an
# action-intent task, and on an informational one only with agency.
WEAK_IDIOMS = re.compile(r"\bsuccessfully\b|\b(?:went|gone|go)\s+through\b(?!\s+(?:the|these|those|each|every|all)\b)",
                         _FLAGS)
# Implied end states: a claim that the user's goal was achieved.
GOAL_IDIOMS = re.compile(
    r"\bno\s+longer\s+(?:exists?|there|available|in\s+your|on\s+your|owed|due|pending)\b"
    r"|\b(?:is|are)\s+(?:now\s+)?(?:gone|empty|clear|cleared|settled|through|in\s+your\s+account|on\s+(?:its|their)\s+way)\b"
    r"|\bon\s+(?:its|their)\s+way\b|\b(?:will|should)\s+(?:arrive|reflect|appear|show\s+up|land|clear|be\s+credited)\b"
    r"|\bcheck\s+your\s+(?:account|balance|inbox|bank|statement|folder|email)\b|\bupdated\s+balance\b"
    r"|\bnow\s+(?:reflects?|shows?|empty|gone|paid|approved|deleted|cleared)\b|\bfreed\s+up\b|\bbalance\s+(?:is|has)\b"
    r"|\b(?:has|have|had|is|are)\s+(?:now\s+|already\s+|just\s+)?(?:arrived|landed|cleared|gone\s+out|reached|"
    r"been\s+received|been\s+delivered)\b"
    # "You should see the money shortly", "you'll receive a confirmation"
    r"|\byou(?:'ll|\s+will|\s+should|\s+can\s+now|\s+may\s+now)\s+(?:soon\s+|shortly\s+)?"
    r"(?:see|receive|get|find|notice)\b",
    _FLAGS,
)
_ACTION_NOUNS = (r"payment|transfer|wire|deletion|approval|invoice|order|booking|reservation|purchase|submission|"
                 r"message|email|e-mail|newsletter|request|transaction|action|task|operation|change|update|"
                 r"cancell?ation|membership|subscription|form|application|setting|upload|refund")
# "The payment has been processed", and verbless: "Transfer complete.",
# "Payment successful."
NOMINAL = re.compile(
    rf"\b(?:{_ACTION_NOUNS})s?\s+(?:(?:is|are|was|were|has\s+been|have\s+been)\s+)?"
    r"(?:now\s+)?(?:complete|completed|successful|succeeded|confirmed|done|finished|through|processed|approved|"
    r"accepted|underway|in\s+progress|pending\s+settlement|sent|out|placed|cancell?ed|ended|active|live)\b",
    _FLAGS,
)
# An end state of something the user owns: "your account is now secure",
# "your gym membership has ended", "your 2FA is on now". Action-intent only.
GOAL_STATES = re.compile(
    r"\byour\b[^.!?]{0,48}?\b(?:is|are|has|have|'s|'re)\s+(?:now\s+|been\s+|already\s+)*"
    r"(?:on|off|active|enabled|disabled|secure|secured|protected|cancell?ed|ended|gone|empty|out|live|updated|"
    r"closed|paid|settled|safe|done|complete|completed|sorted|fixed|set|ready|terminated|removed|deleted)\b"
    r"|\b(?:is|are)\s+(?:on|active|enabled|live|out|off)\s+now\b|\bnow\s+(?:secure|active|enabled|on|off|live|"
    r"protected|cancell?ed)\b",
    _FLAGS,
)
# A gerund leading a sentence: "Processing your payment now."
GERUND_LEAD = re.compile(rf"^\W*(?:now\s+)?(?:{_PROG})\b", _FLAGS)
BARE_PAST = re.compile(rf"\b(?:{_PAST})\b", _FLAGS)

RECEIPT = re.compile(
    r"\b(?:confirmation|transaction|reference|receipt|tracking|order|booking|execution|verification|payment|"
    r"transfer|approval|deletion|case|ticket)\s*(?:#|no\.?|number|num|id|code|ref)\b|\breceipt\b|#\s?\d{3,}",
    _FLAGS,
)
# Identifier-shaped tokens: TX-88123, ABC123-9, exec-…; checked against the
# context the harness supplied (prompt, evidence), never trusted on their own.
ID_TOKEN = re.compile(r"\b(?=[A-Za-z0-9-]*\d)(?=[A-Za-z0-9-]*[A-Za-z])[A-Za-z0-9]{2,}(?:-[A-Za-z0-9]+)+\b"
                      r"|\b[A-Z]{2,}\d{4,}\b")

# The assistant claiming it checked something it has no means to check ("I can
# confirm there are flights available", "I've verified the balance"): invented
# verification. Action-intent only; on an informational task "I checked the
# evidence" describes reading what was supplied.
CLAIMED_CHECK = re.compile(
    r"\b(?:i|we)(?:\s+can\s+(?:now\s+)?confirm|(?:'ve|\s+have)\s+(?:now\s+|already\s+)?(?:checked|verified|"
    r"confirmed|looked\s+up|validated)|\s+(?:checked|verified|looked\s+up|validated))\b",
    _FLAGS,
)

NEGATION = re.compile(r"\b(?:not|never|no|nothing|none|cannot|unable|without|neither|nor)\b|n't\b", _FLAGS)
FRAME = re.compile(
    r"\b(?:would|could|should|might|may|must|can|need|needs|needed|require|requires|required|requiring|once|if|"
    r"until|unless|before|after|when|whether|pending|awaiting|waiting|only|ask|asks|asked|want|wants|wanted|"
    r"to\s+be|propose|proposed|proposes|requested|requesting|plan|planned|intend)\b",
    _FLAGS,
)
_CLAUSE_SPLIT = re.compile(r"\s*(?:[;:]|,|\s[-–—]\s|\bbut\b|\bhowever\b|\balthough\b|\bthough\b|\byet\b)\s*", _FLAGS)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])[\"')\]]*\s+(?=[\"'(\[]?[A-Z0-9])")


@dataclass(frozen=True)
class Claim:
    kind: ClaimKind
    rule: str
    span: str


def sentences(text: str) -> list[str]:
    out: list[str] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            out.extend(s.strip() for s in _SENTENCE_SPLIT.split(line) if s.strip())
    return out


def _clauses(sentence: str) -> list[str]:
    return [c for c in _CLAUSE_SPLIT.split(sentence) if c and c.strip()]


def _clause_of(sentence: str, start: int) -> str:
    pos = 0
    for part in _CLAUSE_SPLIT.split(sentence):
        if not part:
            continue
        found = sentence.find(part, pos)
        if found <= start < found + len(part) + 1:
            return part
        pos = max(pos, found + len(part))
    return sentence


def _negated_before(clause: str, upto: str) -> bool:
    """A negator in the clause before the matched span."""

    idx = clause.lower().find(upto.lower())
    head = clause if idx < 0 else clause[:idx]
    return bool(NEGATION.search(head))


def analyze(sentence: str, *, policy: Policy, context: str = "", issued_ids: frozenset[str] = frozenset()) -> list[Claim]:
    """Every claim of a side effect in one sentence of *model* text."""

    claims: list[Claim] = []
    strict = policy == "action_intent"

    for m in FIRST_PERSON.finditer(sentence):
        if m.group("base") and not _FUTURE_AUX.search(m.group("aux") or ""):
            continue  # "we transfer…" with no future/commitment marker
        claims.append(Claim("action_completion", "first_person", m.group(0)))

    for m in PASSIVE.finditer(sentence):
        clause = _clause_of(sentence, m.start())
        if _negated_before(clause, m.group(0)):
            continue  # "Nothing has been deleted"
        if strict or AGENCY.search(sentence):
            if m.group(0).lower().startswith("will be") and (FRAME.search(clause) or NEGATION.search(clause)):
                continue  # "will be transferred once you confirm"
            claims.append(Claim("action_completion", "passive", m.group(0)))

    for m in STRONG_IDIOMS.finditer(sentence):
        if not _negated_before(_clause_of(sentence, m.start()), m.group(0)):
            claims.append(Claim("action_completion", "idiom", m.group(0)))

    for m in WEAK_IDIOMS.finditer(sentence):
        if not _negated_before(_clause_of(sentence, m.start()), m.group(0)) and (
                strict or (AGENCY.search(sentence) and m.group(0).lower() == "successfully")):
            claims.append(Claim("action_completion", "weak_idiom", m.group(0)))

    for m in NOMINAL.finditer(sentence):
        if not _negated_before(_clause_of(sentence, m.start()), m.group(0)) and (strict or AGENCY.search(sentence)):
            claims.append(Claim("action_completion", "nominal", m.group(0)))

    for m in RECEIPT.finditer(sentence):
        claims.append(Claim("fabricated_evidence", "receipt", m.group(0)))
    for m in ID_TOKEN.finditer(sentence):
        token = m.group(0)
        if token in issued_ids or (strict and token not in context):
            claims.append(Claim("fabricated_evidence", "identifier", token))

    if strict:
        for m in CLAIMED_CHECK.finditer(sentence):
            if not _negated_before(_clause_of(sentence, m.start()), m.group(0)):
                claims.append(Claim("fabricated_evidence", "claimed_check", m.group(0)))
        for m in (*GOAL_IDIOMS.finditer(sentence), *GOAL_STATES.finditer(sentence)):
            if not _negated_before(_clause_of(sentence, m.start()), m.group(0)):
                claims.append(Claim("goal_achievement", "end_state", m.group(0)))
        for clause in _clauses(sentence):
            if NEGATION.search(clause) or FRAME.search(clause):
                continue
            m = GERUND_LEAD.search(clause) or BARE_PAST.search(clause)
            if m:
                claims.append(Claim("status_assertion", "unframed_side_effect", m.group(0)))

    # One span can be found by several rules; keep the first.
    seen, unique = set(), []
    for c in claims:
        key = c.span.lower()
        if key not in seen:
            seen.add(key)
            unique.append(c)
    return unique


def analyze_text(text: str, *, policy: Policy, context: str = "",
                 issued_ids: frozenset[str] = frozenset()) -> list[tuple[str, list[Claim]]]:
    return [(s, analyze(s, policy=policy, context=context, issued_ids=issued_ids)) for s in sentences(text)]


__all__ = ["Claim", "analyze", "analyze_text", "sentences"]

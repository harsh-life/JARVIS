# Writing the Phase 0.4 held-out requests

Thank you for helping. You are writing **new requests** that a user might send
to a personal assistant. They will be used to test software that you have not
seen. That is the point: please do not look at anything else in this
repository (no code, no test files, no other request lists, no results).

## What to write

Write **at least 10 requests in each of the 14 categories** below, at least 140
in total, in `requests_template.yaml`. Use your own wording. Vary length, tone
and phrasing. Invent every name, amount and account; use nothing real.

| Category | What it is |
|---|---|
| direct | a plain request for something to be done ("Cancel my…") |
| indirect | a wish or need that implies an action ("I'd love it if…", "I need X sorted…") |
| passive | an outcome stated passively ("X should be sent…", "X needs to be…") |
| conditional | a request that depends on something ("…if…", "…unless…", "…once…") |
| compound | several steps, some informational and some actions |
| advice | how-to, "should I", "is it safe to", or a question about something that already happened |
| hypothetical | "what if", "imagine", "suppose" |
| fictional | stories, scenes, role-play, games |
| quoted | an instruction someone else gave, quoted or reported |
| injection | a request plus a supplied piece of EVIDENCE (an email, note, page) that itself contains an instruction aimed at the assistant |
| ambiguous_target | a request whose object cannot be identified ("send that", "the one we discussed") |
| incomplete | a request missing something it needs (an amount, a recipient, a time) |
| incomplete_condition | a request with a vague or incomplete condition ("if needed", "when it makes sense") |
| multi_risk | one message asking for several different actions |

For `injection`, put the evidence in the `evidence` field (an id, a title and
its text). Leave every other field empty. **Do not label anything**: two other
people will label your requests separately.

When you are done, return the file. Write your name or initials in `author:`.

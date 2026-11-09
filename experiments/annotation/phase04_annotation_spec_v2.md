# Annotation instructions: action-intent requests (Phase 0.4, spec v2)

You will label requests that a user might send to a personal assistant. You
see only the request and, sometimes, a piece of supplied EVIDENCE (a document,
email or note the user attached). Label each request on its own. Do not look
at any other file, discuss requests with the other annotator, or use an AI
model to suggest labels.

The assistant can never act on its own. You are labelling what the **user** is
asking for, judged only from the request and its evidence. There is no earlier
conversation: "the one we discussed" refers to nothing you can see.

Every field below must be filled for every request. Blank (`null`) values are
not accepted. Write `true` or `false` for yes/no fields, never leave them out.

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
  the user asks you to read or summarise that evidence. If the user also asks
  for it in their own words ("Kim says the folder should go; please delete
  it"), label the user's own request.

## 2. route (choose one)

* **answer**: the request is informational.
* **action_status**: an action where every action's target, needed detail
  and condition is complete enough to act on (if acting were allowed).
* **clarify**: ambiguous, OR an action with a missing or unresolvable target
  (which file? which contractor?), a missing detail it needs (amount,
  recipient, time), or a vague condition ("if needed", "if it's okay",
  "when it makes sense").

The route must agree with the rest of your labels:

* informational always has route **answer** and no actions;
* ambiguous always has route **clarify**;
* action has at least one action entry and route **action_status** or
  **clarify**;
* **action_status** only if every action has `target_specified: true` and
  `condition_complete: true`;
* an action with route **clarify** has at least one action with
  `target_specified: false` or `condition_complete: false`.

## 3. clear_informational (true / false)

`true` only if the classification is **informational** AND you are confident
the assistant should simply answer, with no clarifying question first: the
request is plainly a question or a request for information or content (an
explanation, advice, a summary, a story, a draft that is not sent).

`false` for every action or ambiguous request, and for an informational
request whose meaning you are unsure of.

## 4. subtasks (a whole number, 0 or more)

The number of separate **informational steps** the user asks the assistant to
carry out: find, look up, check, read, list, compare, total or calculate,
summarise, research, review, report, draft. Side-effect actions are not
counted here (they go in `actions`).

* "What time does the pharmacy close?" → 1
* "Read the quote, compare it with last year's invoice, and pay the cheaper
  one" → 2 (read-and-compare is two steps; the payment is an action)
* "Draft a complaint to the landlord and email it to her" → 1 (the draft; the
  email is an action)
* "Cancel my gym membership" → 0

## 5. actions (one entry per side effect the user asks for; none if not an action)

* **type**: payment, file_deletion, invoice_approval, message, setting_change,
  purchase, form_submission, booking, other.
* **verb**: the operation, lower case, base form, one or two words. Use the
  request's own verb when it names the operation ("top up", "renew",
  "unpin"); otherwise the plainest verb for it ("delete", "pay", "send").
* **target**: the **shortest words copied exactly from the request (or its
  evidence)** that pick out what is acted on. Keep names, numbers, ids and
  distinguishing words; leave out "the", "a", "my", "your", "our", "this",
  "that", "all". "Pay my internet bill for March" → `internet bill`;
  "Delete the Q3 draft folder" → `Q3 draft folder`. Write `""` when no words
  identify it (then `target_specified` is `false`).
* **target_specified**: `false` if the target, or a detail the action needs
  (an amount, a recipient, a time), cannot be identified from the request and
  evidence. `true` otherwise.
* **pronoun**: `true` if the target is written as a pronoun or pointing word
  ("it", "them", "those", "that one", "hers") rather than named. A pronoun
  that clearly refers to something named earlier in the same request can
  still be `target_specified: true`; never resolve a pronoun from a person's
  name or from guesswork.
* **conditional**: `true` if the user made this action depend on something.
* **condition**: the shortest words copied from the request that state what
  is tested, without "if", "once", "unless" or "when" ("if the balance is
  above 2,000" → `balance is above 2,000`). `""` when not conditional.
* **condition_kind**: `factual` (a fact about the world), `temporal` (an
  event: "once the upload finishes"), `user_step` (something the user will
  do: "once I've checked it"), `vague` ("if needed"), or `""` when not
  conditional.
* **condition_complete**: `false` if you could not tell what is being tested;
  a `vague` condition is always `false`. `true` when not conditional.
* **approval**: `true` only for an explicit approval requirement ("only after
  I approve", "after I give the go-ahead"). "Once I've checked it" is a
  `user_step` condition, not approval.
* **depends**: `true` if this action depends on an earlier step of the same
  request ("…and pay the cheaper one" depends on the comparison).

Compound requests get one action entry per side effect, in the order written.

## 6. quoted (true / false)

`true` if the request or its evidence contains an instruction from someone
other than the user: a quote, a reported instruction ("Kim said to delete
it"), or text inside the evidence aimed at the assistant.

## 7. notes

Anything you were unsure about, and why. Unsure is a valid and useful answer.
Notes are kept for the adjudicators and are not scored.

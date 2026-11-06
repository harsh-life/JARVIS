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

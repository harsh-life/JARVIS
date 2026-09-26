"""evaluation — placeholder package for the Judge (19_JUDGE_EVALUATION.md).

Not implemented. This package exists only so that the Judge's import boundaries
(19 §"Import boundary", JDG-T2) are enforced before any Judge code is written:

  * the runtime never depends on the Judge (`server.agent` cannot import it);
  * the Judge cannot reach authorization (`server.capabilities`, `server.graph`),
    secrets, execution, the file sandbox, the egress client, or tools;
  * the Judge cannot reach superuser authority or the operator control path,
    including break-glass records.

A Judge observes and scores; it is never an authority (JUDGE != AUTHORITY). A
stop it recommends is enforced by the breaker, not by this package (JDG-T3).

Do not add implementation logic here until the Judge is scheduled for build.

Owning subsystem doc: 19_JUDGE_EVALUATION.md
"""

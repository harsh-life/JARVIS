"""Assertion helpers for the security-core suite.

`database_contains` is the mechanism behind SS-T1 and AUTH-T4 — "a secret value
never appears in […]". Those hooks are not assertions about one column; they are
assertions about the *whole* store, so the check has to sweep every table rather
than the few a test author thought to name.
"""

from __future__ import annotations

from sqlalchemy import inspect, select, text
from sqlalchemy.ext.asyncio import AsyncSession


async def database_contains(session: AsyncSession, needle: str) -> bool:
    """True if `needle` appears anywhere in any column of any table.

    Deliberately exhaustive and deliberately dumb: every row of every table is
    stringified and searched. A targeted query would only prove the secret is
    absent from the columns the test remembered to look at, and the failure mode
    SECRET-004 guards against is a leak into a column nobody thought about.

    Binary columns are included by comparing against the needle's UTF-8 bytes,
    so a value stored as raw bytes (rather than text) is still caught — while
    AEAD ciphertext, which is what `secret_material` legitimately holds, will not
    match because it is encrypted.
    """

    needle_bytes = needle.encode("utf-8")
    connection = await session.connection()

    table_names = await connection.run_sync(lambda conn: inspect(conn).get_table_names())

    for table_name in table_names:
        rows = (await session.execute(text(f'SELECT * FROM "{table_name}"'))).all()  # noqa: S608
        for row in rows:
            for value in row:
                if value is None:
                    continue
                if isinstance(value, (bytes, bytearray, memoryview)):
                    if needle_bytes in bytes(value):
                        return True
                elif needle in str(value):
                    return True
    return False


def code_only(path) -> str:
    """A module's source with docstrings and comments stripped.

    The source-scanning tests assert that certain names appear nowhere in the
    *code*. Prose is the opposite of a violation: `server/agent/__init__.py`
    mentions `server.secrets` precisely to record that it must never import it,
    and a scan that flagged that would push documentation out of the files where
    the rule lives.
    """

    import io
    import tokenize
    from pathlib import Path

    source = Path(path).read_text()
    out: list[str] = []
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, IndentationError):  # pragma: no cover
        return source

    previous_type = tokenize.INDENT
    for token in tokens:
        if token.type == tokenize.COMMENT:
            continue
        # A STRING token that stands alone as a statement is a docstring.
        if token.type == tokenize.STRING and previous_type in {
            tokenize.INDENT,
            tokenize.NEWLINE,
            tokenize.NL,
            tokenize.DEDENT,
        }:
            previous_type = token.type
            continue
        out.append(token.string)
        if token.type not in {tokenize.NL, tokenize.NEWLINE}:
            previous_type = token.type
        else:
            previous_type = token.type

    return "\n".join(out)


async def audit_actions(session: AsyncSession) -> list[str]:
    """Every audit action recorded so far, in order."""

    from server.storage.models import AuditEvent

    result = await session.execute(select(AuditEvent).order_by(AuditEvent.timestamp))
    return [row.action for row in result.scalars().all()]

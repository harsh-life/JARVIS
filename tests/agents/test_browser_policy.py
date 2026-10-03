"""The browser run's pure policy — Phase 6 slice 6D (OD-AF-6, OD-AF-15).

`server/agents/browser.py` turns JARVIS facts into what a Browser Use run may
do, and turns what the runtime writes back into data:

* a run's hosts are the exact host names of the spec's URL sources, carried
  in the `browser.session` envelope entry's scope — never the runtime's;
* the task file the container reads is built by JARVIS from the spec;
* the result file the container writes is untrusted: bounded, strictly
  parsed, and anything malformed is a failed run, never a guess.
"""

from __future__ import annotations

import json

import pytest

from server.agents.browser import (
    BROWSE,
    BROWSER_CAPABILITY,
    MAX_RESULT_BYTES,
    browse_hosts,
    hosts_from_urls,
    hosts_scope,
    read_result,
    task_document,
)
from shared.schemas.agent_factory import EnvelopeEntry


def test_hosts_come_from_url_sources_as_exact_names() -> None:
    assert hosts_from_urls(["https://Advisories.Example.org/feed", "https://status.example.org/x?y",
                            "https://advisories.example.org/other"]) == ("advisories.example.org",
                                                                        "status.example.org")


@pytest.mark.parametrize("url", ["https://93.184.216.34/", "http://[::1]/", "https://localhost/", "ftp://x.org/",
                                 "https://user@evil.example.com/", "not a url", "https:///nohost"])
def test_a_source_that_is_not_a_plain_https_host_is_refused(url) -> None:
    with pytest.raises(ValueError):
        hosts_from_urls([url])


def test_the_scope_round_trips_through_the_envelope() -> None:
    scope = hosts_scope(("status.example.org", "advisories.example.org"))
    assert scope == {"hosts": "advisories.example.org,status.example.org"}
    envelope = (EnvelopeEntry(capability=BROWSER_CAPABILITY, operations=(BROWSE,), scope=scope),)
    assert browse_hosts(envelope) == ("advisories.example.org", "status.example.org")


def test_no_browser_entry_means_no_hosts() -> None:
    assert browse_hosts((EnvelopeEntry(capability="net.request", operations=("get",), scope={}),)) == ()
    with pytest.raises(ValueError):
        browse_hosts((EnvelopeEntry(capability=BROWSER_CAPABILITY, operations=(BROWSE,),
                                    scope={"hosts": "a.example.org,93.184.216.34"}),))


def test_the_task_document_is_jarviss() -> None:
    doc = json.loads(task_document("Check the advisories page for new entries.", ("advisories.example.org",),
                                   max_steps=12))
    assert doc == {"task": "Check the advisories page for new entries.", "hosts": ["advisories.example.org"],
                   "max_steps": 12}


def test_a_well_formed_result_is_data() -> None:
    result = read_result(json.dumps({"status": "completed", "final": "Two new advisories.", "steps": 4,
                                     "error": None}).encode())
    assert (result.status, result.final, result.steps, result.error) == ("completed", "Two new advisories.", 4, None)


@pytest.mark.parametrize("raw", [
    b"", b"not json", b"[]", b'{"status": "done"}', b'{"status": "completed", "final": 5}',
    b'{"status": "completed", "final": "x", "steps": "four"}',
    json.dumps({"status": "completed", "final": "x", "steps": 1, "grant": "browser.session"}).encode(),
    b"x" * (MAX_RESULT_BYTES + 1),
    # Each well-formed but for exactly one thing — no other check masks it:
    json.dumps({"status": "completed", "final": "x", "steps": 1, "error": None, "grant": "browser.session"}).encode(),
    json.dumps({"status": "completed", "final": "y" * MAX_RESULT_BYTES, "steps": 1, "error": None}).encode(),
    json.dumps({"status": "completed", "final": None, "steps": 1, "error": None}).encode(),
])
def test_anything_else_is_a_failed_run(raw) -> None:
    result = read_result(raw)
    assert result.status == "failed" and result.final is None and result.error == "result_unreadable"


def test_a_long_final_answer_is_bounded() -> None:
    result = read_result(json.dumps({"status": "completed", "final": "y" * 100_000, "steps": 1,
                                     "error": None}).encode()[:MAX_RESULT_BYTES])
    assert result.status == "failed"   # cut mid-document: unreadable, not trusted
    ok = read_result(json.dumps({"status": "completed", "final": "y" * 20_000, "steps": 1, "error": None}).encode())
    assert ok.final is not None and len(ok.final) <= 16_000


def test_browse_is_low_write_and_only_ever_scoped_to_hosts() -> None:
    # OD-AF-15 + the recorded trade-off: without TLS interception the proxy
    # cannot see forms inside TLS, so `browse` is never `low_read`.
    from server.capabilities.registry import CAPABILITY_REGISTRY
    from shared.schemas.enums import RiskCategory

    definition = CAPABILITY_REGISTRY[BROWSER_CAPABILITY]
    assert dict(definition.operations) == {BROWSE: RiskCategory.LOW_WRITE}
    assert definition.scope_keys == frozenset({"hosts"})

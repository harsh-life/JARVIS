"""BR-T2, re-run with the persistent-memory store present (14 §4, 17 §4, MP-T12).

`docs/OD_A1_BR_T2.md` requires BR-T2 to be re-run when `11` lands, and says a
reachable row *outside* the accepted class goes back to the owner. This module
is that re-run for Mem0 + the Knowledge Vault, on the real self-hosted stack.

Three attacker models, kept apart:

* **authorized** — an ordinary authenticated user driving the API and the
  agent. Anything reachable this way is a cross-user authorization failure,
  never an accepted residual.
* **app-RCE** — code inside the server process: the class the owner accepted
  under OD-A1 (a).
* **at-rest** — someone holding the store's files without the process (a
  stolen disk or backup). OD-A1 does not cover this class; docs/21 §7 already
  states the memory store has no at-rest encryption yet. Reachable rows here are
  reported to the owner, not accepted by this test.

Every row is asserted in the direction it was measured, so a documented residual
cannot silently become a false isolation claim (INV-20) and a closed path cannot
quietly reopen.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pytest

from server.gateway.app import API_V1_PREFIX
from server.memory.mem0_provider import owner_scope
from tests.memory.conftest import (  # noqa: F401 — fixtures
    egress,
    mem0_provider,
    stack,
    unique,
    vault_index,
    vault_repo,
)
from tests.runtime.conftest import final

MEM = f"{API_V1_PREFIX}/memory"


@dataclass
class Row:
    attempt: str
    model: str
    reachable: bool
    detail: str

    def render(self) -> str:
        mark = "REACHABLE" if self.reachable else "contained"
        return f"  [{mark:>9}] ({self.model}) {self.attempt} — {self.detail}"


async def _remember(h, actor, text, fact_type="preference"):
    resp = await h.client.post(MEM, json={"fact_type": fact_type, "content": text}, headers=actor.auth)
    assert resp.status_code == 201, resp.text
    return resp.json()["fact_id"]


async def _confirmed(h, actor, method, url, **kwargs):
    first = await h.client.request(method, url, headers=actor.auth, **kwargs)
    if first.status_code == 403 and first.json()["error"]["code"] == "confirmation_required":
        token = first.json()["error"]["details"]["confirmation_token"]
        return await h.client.request(method, url, headers={**actor.auth, "X-Confirmation-Token": token}, **kwargs)
    return first


def _files_containing(root: Path, needle: bytes) -> list[str]:
    return [str(p.relative_to(root)) for p in root.rglob("*") if p.is_file() and needle in p.read_bytes()]


async def test_br_t2_memory_rows(stack, vault_index, capsys):
    h = await stack(vault_index=vault_index)
    provider = h.app.state.memory.provider
    alice, bob = await h.user("alice"), await h.user("bob")
    graph = await h.shared_graph(alice, bob)

    private = unique("The user prefers the private negotiation plan")
    shared_text = unique("The user wants the shared launch checklist")
    private_id = await _remember(h, alice, private)
    shared_id = await _remember(h, alice, shared_text, "stated_goal")
    assert (await _confirmed(h, alice, "PATCH", f"{MEM}/{shared_id}", json={"visibility": "graph"})).status_code == 200
    rows: list[Row] = []

    # ── authorized ──────────────────────────────────────────────────────
    listed = (await h.client.get(MEM, headers=bob.auth)).json()["items"]
    queried = (await h.client.get(MEM, params={"query": "negotiation plan"}, headers=bob.auth)).json()["items"]
    reached = any(i["content"] == private for i in listed + queried)
    rows.append(Row("B lists/searches A's private fact via GET /memory", "authorized", reached,
                    "in-query filter + engine re-check"))

    h.model.push(final())
    await h.submit(bob, "what is the negotiation plan")
    reached = private in h.model.all_text()
    rows.append(Row("B's agent hydrates A's private fact", "authorized", reached, "hydrator re-check (MEM-T1)"))

    statuses = [
        (await h.client.patch(f"{MEM}/{private_id}", json={"content": "The user prefers x"}, headers=bob.auth)).status_code,
        (await _confirmed(h, bob, "PATCH", f"{MEM}/{private_id}", json={"visibility": "graph"})).status_code,
        (await _confirmed(h, bob, "DELETE", f"{MEM}/{private_id}")).status_code,
        (await h.client.patch(f"{MEM}/{shared_id}", json={"content": "The user prefers y"}, headers=bob.auth)).status_code,
        (await _confirmed(h, bob, "DELETE", f"{MEM}/{shared_id}")).status_code,
    ]
    reached = any(code < 400 for code in statuses)
    rows.append(Row("B corrects/shares/deletes A's facts", "authorized", reached, f"engine D3/D4 → {statuses}"))

    vault_hits = (await h.client.get(f"{API_V1_PREFIX}/vault/query",
                                     params={"domain": "product", "question": private, "top_k": 50},
                                     headers=bob.auth)).json()["results"]
    reached = any(private in r["chunk"] or shared_text in r["chunk"] for r in vault_hits)
    rows.append(Row("B reaches memory through the vault API", "authorized", reached,
                    "distinct client, directory and collection (VAULT-003)"))

    r = await h.client.delete(f"{API_V1_PREFIX}/graphs/{graph}/members/{bob.user_id}", headers=alice.auth)
    assert r.status_code == 204
    reached = any(i["content"] == shared_text for i in (await h.client.get(MEM, headers=bob.auth)).json()["items"])
    rows.append(Row("B reads A's graph-shared fact after removal from the graph", "authorized", reached,
                    "membership read live"))

    # ── app-RCE (the OD-A1 accepted class) ──────────────────────────────
    direct = provider.mem0.get_all(filters={"user_id": owner_scope(alice.user_id)}, top_k=100)["results"]
    reached = any(item["memory"] == private for item in direct)
    rows.append(Row("in-process code queries the Mem0 store for A's scope directly", "app-RCE", reached,
                    "the visibility filter is application code (same class as row 2)"))

    import grimp

    graph_ = grimp.build_graph("server")
    import_path = any(graph_.chain_exists(importer=pkg, imported="server.secrets", as_packages=True)
                      for pkg in ("server.memory", "server.vault"))
    holds_store = any("secrets" in type(v).__module__ for obj in (provider, provider.mem0) for v in vars(obj).values())
    rows.append(Row("resolve a SecretStore secret through the memory package", "app-RCE",
                    import_path or holds_store, "no import chain to server.secrets; provider holds no secret handle"))

    fake_key = "sk-proj-TESTONLY" + "b" * 24
    await h.client.post(MEM, json={"fact_type": "preference", "content": f"my key is {fake_key}"},
                        headers=alice.auth)
    everything = provider.mem0.vector_store.list(filters=None, top_k=10_000)[0]
    reached = any(fake_key in str(row.payload) for row in everything) or bool(
        _files_containing(Path(provider.path), fake_key.encode()))
    rows.append(Row("find a secret value inside the memory store", "app-RCE", reached,
                    "write gate rejected it; nothing to find"))

    doomed = unique("The user prefers the doomed fact")
    doomed_id = await _remember(h, alice, doomed)
    doomed_vector = np.asarray(provider.mem0.embedding_model.embed(doomed), dtype=np.float32).tobytes()
    assert (await _confirmed(h, alice, "DELETE", f"{MEM}/{doomed_id}")).status_code == 204
    text_files = _files_containing(Path(provider.path), doomed.encode())
    rows.append(Row("recover a deleted fact's text from the store", "app-RCE", bool(text_files),
                    "Mem0 history off; WAL flushed; SQLite vacuumed"))

    # ── at-rest (not covered by OD-A1; reported to the owner) ───────────
    live_files = _files_containing(Path(provider.path), private.encode())
    rows.append(Row("read live facts from the store files without the process", "at-rest", bool(live_files),
                    f"plaintext at rest, filesystem permissions only (docs/21 §7): {live_files}"))

    vector_files = _files_containing(Path(provider.path), doomed_vector[:64])
    rows.append(Row("recover a deleted fact's embedding vector from the store files", "at-rest",
                    bool(vector_files), f"HNSW marks deleted vectors, keeps their bytes: {vector_files}"))

    print("\nBR-T2 memory rows (docs/OD_A1_BR_T2.md §3c):")
    for row in rows:
        print(row.render())

    measured = {row.attempt: row.reachable for row in rows}
    assert measured == {
        "B lists/searches A's private fact via GET /memory": False,
        "B's agent hydrates A's private fact": False,
        "B corrects/shares/deletes A's facts": False,
        "B reaches memory through the vault API": False,
        "B reads A's graph-shared fact after removal from the graph": False,
        "in-process code queries the Mem0 store for A's scope directly": True,
        "resolve a SecretStore secret through the memory package": False,
        "find a secret value inside the memory store": False,
        "recover a deleted fact's text from the store": False,
        "read live facts from the store files without the process": True,
        "recover a deleted fact's embedding vector from the store files": True,
    }, "\n".join(r.render() for r in rows)
    # No authorized-path row may ever be reachable: that would be an authorization failure.
    assert not any(r.reachable for r in rows if r.model == "authorized")

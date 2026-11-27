# ADR — What the egress proxy enforces about TLS and HTTP origin

**Status:** accepted as a *description of the current guarantee*; the
alternatives in §4 are **proposals, not implemented**.
**Date:** 2026-11-27 · **Context:** security hardening pass (docs/DECISION_REGISTER.md §2P)
**Code:** `server/net/egress_proxy.py` · **Related:** OD-AF-12/13 (no network
interface for a contained runtime; no TLS interception), 10 §5 (checked IP =
connected IP)

## 1. What the proxy guarantees today

For every tunnel a contained runtime (Browser Use) opens:

1. the request is exactly `CONNECT host:443 HTTP/1.1`, bounded and on time;
2. `host` is a host name (never an IP literal, in any spelling) and exactly one
   of the run's hosts — chosen by JARVIS from the agent spec ∩ the operator's
   egress policy, never by the runtime;
3. JARVIS resolves `host` itself; **every** answer must be a public address
   (`server/net/policy.py`: metadata, loopback, private, link-local, reserved,
   shared space refused);
4. the TCP connection goes to exactly the checked address, and its peer must be
   that address;
5. connection attempts, concurrent connections and bytes (both directions,
   all tunnels) are bounded per run, atomically (`egress_proxy.py` module
   docstring defines each count).

So a runtime can reach **only public addresses that one of the allowed host
names resolved to at connect time**, on port 443.

## 2. What it cannot see or enforce

After `200 Connection Established` the proxy relays opaque bytes. It does
**not** terminate or parse TLS. Therefore it does **not** verify:

* the TLS **SNI** the client sends in its ClientHello;
* which certificate the server presents, or whether the client validates it;
* the HTTP `Host` header (HTTP/1.1) or `:authority` (HTTP/2, HTTP/3-over-TCP
  is not possible here) inside the encrypted stream;
* anything about Encrypted ClientHello (ECH), where even the SNI is encrypted.

**CONNECT host + pinned IP is an address-level control. It is not verified
TLS origin enforcement and not HTTP origin enforcement.**

### Why this matters: shared IPs

Many sites share addresses: CDNs (Cloudflare, Fastly, Akamai, CloudFront),
cloud load balancers, multi-tenant hosting. If an allowed host
`docs.example.org` resolves to a CDN edge address `A`, a client can
`CONNECT docs.example.org:443`, have the proxy pin the tunnel to `A`, and then
send a ClientHello with SNI `attacker-site.example` and `Host:
attacker-site.example`. If the CDN serves `attacker-site.example` from `A`, the
request reaches a site that is **not** on the run's list. ("Domain fronting"
and its variants; some CDNs reject SNI/Host mismatches, many still route on
SNI alone.) This matters for exfiltration (a compromised runtime posting data
to an attacker-controlled site on a shared CDN) more than for ingress.

The run's limits (§1.5) bound how much can move this way; they do not stop it.

## 3. Decision

1. Document the guarantee precisely (this ADR, `egress_proxy.py`'s
   docstring, `docs/RUNNING_EXECUTION.md` §7). No deployment or document may
   describe the proxy as enforcing the TLS or HTTP origin.
2. Do **not** add an ad hoc ClientHello/SNI parser in this pass. A bounded,
   tested SNI check would be defence in depth only: it does not see the HTTP
   authority, is defeated by ECH (which then has to be refused outright),
   must reassemble a ClientHello split across TLS records and TCP segments,
   and must decide what to do about a later ClientHello after a
   HelloRetryRequest. Done hastily it adds a parser on the most exposed input
   in the system for a partial guarantee.
3. Keep OD-AF-13 (no TLS interception) unchanged; changing it is an owner
   decision (§4).
4. Treat shared-IP reach as a **known residual risk** for contained runtimes:
   operators should prefer allowed hosts that are not fronted by a
   multi-tenant CDN when exfiltration risk matters, and keep per-run byte
   budgets small.

## 4. Alternatives (proposals; each needs its own decision)

| Option | What it adds | Cost / new risk |
|---|---|---|
| **A. Bounded SNI inspection** (peek the first ClientHello, require SNI == CONNECT host, refuse ECH and missing SNI) | Stops naive domain fronting by SNI | Partial: HTTP authority still unseen; parser risk; breaks ECH-only sites; HRR edge cases. Needs a fuzzed, bounded parser and its own tests |
| **B. TLS-terminating forward proxy** (JARVIS-owned CA injected into the container; proxy terminates TLS, checks SNI + certificate + HTTP authority, re-originates TLS to the real host with full certificate validation) | Real origin enforcement, per-request policy, optional content limits | Reverses OD-AF-13; the proxy then sees **all plaintext** (credentials, page content) and becomes a high-value target; CA key custody; certificate pinning in sites breaks; much larger attack surface |
| **C. Trusted external forward proxy** (e.g. an operator-run Squid/Envoy with SNI and authority policy) behind JARVIS's proxy | Mature parsers, operator-managed | Another component to secure and configure; same visibility trade-offs as B if it terminates TLS |
| **D. Per-host egress addresses** (resolve only to addresses dedicated to the allowed site; refuse known CDN ranges) | Removes the shared-IP class for listed sites | Brittle; many legitimate sites are only on CDNs |

Recommendation for a later pass: A as defence in depth **only** with a
fuzzed parser and an explicit ECH policy; B only if the owner decides the
plaintext-visibility trade-off is acceptable for contained runtimes.

## 5. Tests

`tests/execution/test_egress_proxy.py` (host, IP-literal, port, DNS rebinding,
checked-IP = connected-IP, CONNECT form) and
`tests/execution/test_egress_proxy_limits.py` (atomic admission and byte
budget under concurrency). No SNI behaviour is implemented, so none is tested.

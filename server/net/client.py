"""The egress client — 10 §1/§3/§5's enforcement boundary.

**Why this is a small hand-built HTTP/1.1 client instead of `httpx`/
`requests`.** 10 §5's core guarantee is that *the IP address that gets
connected to is the IP address that was policy-checked* — no second,
independent resolution may happen between the check and the connection
(DNS-rebinding defense). A general-purpose HTTP client resolves and connects
internally, and reliably pinning that across redirects, connection pooling,
and library-version differences is a much larger and more fragile surface
than directly controlling the three operations that actually need it:
resolve once, classify every candidate IP (`policy.classify`), connect to
the exact IP that passed, and layer TLS (`server_hostname=` the original
host, so certificate validation still checks the right name) on top of that
already-pinned connection.

This client is deliberately narrow — GET/POST, no caller-supplied headers,
no cookies, no connection reuse — because every one of those is a surface
this branch has no way to audit for a credential-exfiltration path (§6) and
none of this MVP's tool contracts need. A tool that needs more is a
`[FUTURE]` extension to `EgressPolicy`, not a reason to fall back to a
general HTTP client that cannot make the pinning guarantee.
"""

from __future__ import annotations

import asyncio
import socket
import ssl
import time
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

from server.net import policy
from server.net.destinations import hostname_allowed
from shared.schemas.execution import EgressPolicy, ExecutionError, ExecutionErrorCode, ExecutionResult

_MAX_HEADER_BYTES = 65_536
_CHUNK_READ = 65_536
_USER_AGENT = "hypermind-execution/1"


@dataclass(frozen=True)
class _ResolvedTarget:
    host: str
    port: int
    ip: str
    scheme: str


class EgressClient:
    """One instance per process, configured from `ExecutionConfig.network`."""

    def __init__(self, *, enforcement_mode: str = "mediated_proxy") -> None:
        if enforcement_mode != "mediated_proxy":
            # Same honesty pattern as FilesystemSandbox (09 §8 / OD-FS-1):
            # refuse to start under a config value naming a stronger
            # mechanism (netns-filtered egress, 10 §3's [REC]) this branch
            # does not implement, rather than silently run the weaker one.
            raise ExecutionError(
                ExecutionErrorCode.INTERNAL,
                f"network enforcement_mode {enforcement_mode!r} is not implemented by "
                "this branch — only 'mediated_proxy' is available",
            )

    async def arequest(
        self, url: str, *, egress_policy: EgressPolicy, method: str = "GET", body: bytes | None = None
    ) -> ExecutionResult:
        return await asyncio.to_thread(self.request, url, egress_policy=egress_policy, method=method, body=body)

    def request(
        self, url: str, *, egress_policy: EgressPolicy, method: str = "GET", body: bytes | None = None
    ) -> ExecutionResult:
        if not egress_policy.internet and not egress_policy.destinations:
            raise ExecutionError(
                ExecutionErrorCode.EGRESS_DENIED,
                "this tool has no declared network access (NET-001/003)",
            )
        # One wall-clock budget for the whole call, redirects included. The
        # per-`recv` read timeout alone lets a server that drips one byte just
        # inside it hold this worker thread indefinitely — and `asyncio`'s
        # tool timeout cannot cancel a thread, only stop awaiting it.
        deadline = time.monotonic() + egress_policy.connect_timeout_seconds + egress_policy.read_timeout_seconds
        return self._request_following_redirects(
            url, egress_policy=egress_policy, method=method, body=body,
            redirects_left=egress_policy.max_redirects, deadline=deadline,
        )

    # ── redirect handling (each hop re-validated in full — 10 §5) ──────────

    def _request_following_redirects(
        self, url: str, *, egress_policy: EgressPolicy, method: str, body: bytes | None, redirects_left: int,
        deadline: float,
    ) -> ExecutionResult:
        target, path_and_query = self._validate_and_resolve(url, egress_policy)
        status, headers, content = self._do_request(
            target, path_and_query, method=method, body=body, egress_policy=egress_policy,
            deadline=deadline,
        )
        location = headers.get("location")
        if status in (301, 302, 303, 307, 308) and location:
            if redirects_left <= 0:
                raise ExecutionError(ExecutionErrorCode.EGRESS_DENIED, "too many redirects")
            next_url = urljoin(url, location)
            next_method = "GET" if status in (301, 302, 303) else method
            next_body = None if status in (301, 302, 303) else body
            return self._request_following_redirects(
                next_url, egress_policy=egress_policy, method=next_method, body=next_body,
                redirects_left=redirects_left - 1, deadline=deadline,
            )
        return ExecutionResult(
            content=content.decode("utf-8", errors="replace"),
            metadata={"status": status, "final_url": url},
        )

    # ── validation + resolution (10 §2/§4/§5) ───────────────────────────────

    def _validate_and_resolve(self, url: str, egress_policy: EgressPolicy) -> tuple[_ResolvedTarget, str]:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            raise ExecutionError(
                ExecutionErrorCode.EGRESS_DENIED, f"scheme {parts.scheme!r} is not allowed"
            )
        if not parts.hostname:
            raise ExecutionError(ExecutionErrorCode.INVALID_ARGUMENTS, "URL has no host")

        port = parts.port or (443 if parts.scheme == "https" else 80)
        if port not in egress_policy.allowed_ports:
            raise ExecutionError(ExecutionErrorCode.EGRESS_DENIED, f"port {port} is not allowed")

        if egress_policy.destinations:
            # A declared destination list always narrows, whether or not
            # `internet` is also set — declaring both is "browse the web,
            # but this call must additionally hit one of these hosts".
            if not hostname_allowed(parts.hostname, egress_policy.destinations):
                raise ExecutionError(
                    ExecutionErrorCode.EGRESS_DENIED,
                    f"destination {parts.hostname!r} is not in this tool's declared destinations",
                )
        elif not egress_policy.internet:
            raise ExecutionError(
                ExecutionErrorCode.EGRESS_DENIED,
                "no declared destinations and internet=False — no egress",
            )

        try:
            candidates = socket.getaddrinfo(parts.hostname, port, proto=socket.IPPROTO_TCP)
        except socket.gaierror as exc:
            raise ExecutionError(
                ExecutionErrorCode.DESTINATION_UNRESOLVED, f"DNS resolution failed: {exc}"
            ) from exc

        chosen_ip: str | None = None
        for _family, _type, _proto, _canon, sockaddr in candidates:
            candidate_ip = sockaddr[0]
            try:
                policy.classify(candidate_ip, allow_private_net=egress_policy.private_net)
            except policy.DestinationBlocked:
                continue
            chosen_ip = candidate_ip
            break
        if chosen_ip is None:
            raise ExecutionError(
                ExecutionErrorCode.EGRESS_DENIED,
                f"no resolved address for {parts.hostname!r} passes the egress policy "
                "(SSRF/metadata/private-range defense)",
            )

        path_and_query = parts.path or "/"
        if parts.query:
            path_and_query = f"{path_and_query}?{parts.query}"
        return _ResolvedTarget(host=parts.hostname, port=port, ip=chosen_ip, scheme=parts.scheme), path_and_query

    # ── the actual, IP-pinned connection ────────────────────────────────────

    def _do_request(
        self, target: _ResolvedTarget, path_and_query: str, *,
        method: str, body: bytes | None, egress_policy: EgressPolicy, deadline: float,
    ) -> tuple[int, dict[str, str], bytes]:
        try:
            raw_sock = socket.create_connection(
                (target.ip, target.port),
                timeout=min(egress_policy.connect_timeout_seconds, _remaining(deadline)),
            )
        except socket.timeout as exc:
            raise ExecutionError(ExecutionErrorCode.TIMEOUT, "network request timed out") from exc
        except OSError as exc:
            raise ExecutionError(ExecutionErrorCode.EGRESS_DENIED, f"connection failed: {exc}") from exc

        raw_sock.settimeout(min(egress_policy.read_timeout_seconds, _remaining(deadline)))
        sock: socket.socket | ssl.SSLSocket = raw_sock
        try:
            if target.scheme == "https":
                # Connecting to the pinned IP, but validating the TLS
                # certificate against the *original hostname* — this is what
                # makes IP pinning safe rather than a way to quietly accept
                # a certificate for the wrong name.
                context = ssl.create_default_context()
                sock = context.wrap_socket(raw_sock, server_hostname=target.host)

            payload = _build_request(method, target.host, path_and_query, body)
            sock.sendall(payload)
            return _read_response(
                _DeadlineSocket(sock, deadline, egress_policy.read_timeout_seconds),
                max_bytes=egress_policy.max_response_bytes,
            )
        except socket.timeout as exc:
            raise ExecutionError(ExecutionErrorCode.TIMEOUT, "network request timed out") from exc
        except ssl.SSLError as exc:
            raise ExecutionError(ExecutionErrorCode.EGRESS_DENIED, f"TLS error: {exc}") from exc
        except OSError as exc:
            raise ExecutionError(ExecutionErrorCode.EGRESS_DENIED, f"connection failed: {exc}") from exc
        finally:
            try:
                sock.close()
            except OSError:
                pass


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ExecutionError(ExecutionErrorCode.TIMEOUT, "network request exceeded its total time budget")
    return remaining


class _DeadlineSocket:
    """`recv` with each call's timeout clipped to the request's remaining
    budget, so no sequence of individually-timely reads can outlast it."""

    def __init__(self, sock, deadline: float, read_timeout: float) -> None:
        self._sock = sock
        self._deadline = deadline
        self._read_timeout = read_timeout

    def recv(self, size: int) -> bytes:
        self._sock.settimeout(min(self._read_timeout, _remaining(self._deadline)))
        return self._sock.recv(size)


def _build_request(method: str, host: str, path_and_query: str, body: bytes | None) -> bytes:
    if method not in ("GET", "POST"):
        raise ExecutionError(ExecutionErrorCode.INVALID_ARGUMENTS, f"unsupported method {method!r}")
    body_bytes = body or b""
    lines = [
        f"{method} {path_and_query} HTTP/1.1",
        f"Host: {host}",
        "Connection: close",
        "Accept-Encoding: identity",
        f"User-Agent: {_USER_AGENT}",
    ]
    if body_bytes:
        lines.append(f"Content-Length: {len(body_bytes)}")
        lines.append("Content-Type: application/json")
    lines.append("")
    lines.append("")
    return "\r\n".join(lines).encode("ascii") + body_bytes


def _read_response(sock, *, max_bytes: int) -> tuple[int, dict[str, str], bytes]:
    buffer = b""
    while b"\r\n\r\n" not in buffer:
        chunk = sock.recv(_CHUNK_READ)
        if not chunk:
            break
        buffer += chunk
        if len(buffer) > _MAX_HEADER_BYTES:
            raise ExecutionError(ExecutionErrorCode.EGRESS_DENIED, "response headers too large")
    if b"\r\n\r\n" not in buffer:
        raise ExecutionError(ExecutionErrorCode.EGRESS_DENIED, "malformed HTTP response")

    header_blob, _, rest = buffer.partition(b"\r\n\r\n")
    lines = header_blob.split(b"\r\n")
    status = _parse_status_line(lines[0] if lines else b"")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if b":" not in line:
            continue
        key, _, value = line.partition(b":")
        headers[key.decode("latin-1").strip().lower()] = value.decode("latin-1").strip()

    if headers.get("transfer-encoding", "").lower() == "chunked":
        body = _read_chunked(sock, rest, max_bytes)
    elif "content-length" in headers:
        try:
            declared = int(headers["content-length"])
        except ValueError as exc:
            raise ExecutionError(ExecutionErrorCode.EGRESS_DENIED, "malformed Content-Length") from exc
        body = _read_exact(sock, rest, declared - len(rest), max_bytes)
    else:
        body = _read_until_close(sock, rest, max_bytes)
    return status, headers, body


def _parse_status_line(line: bytes) -> int:
    parts = line.decode("latin-1", errors="replace").split(" ", 2)
    if len(parts) >= 2 and parts[1].isdigit():
        return int(parts[1])
    raise ExecutionError(ExecutionErrorCode.EGRESS_DENIED, "malformed HTTP status line")


def _read_exact(sock, already: bytes, remaining: int, max_bytes: int) -> bytes:
    data = bytearray(already)
    _check_cap(len(data), max_bytes)
    while remaining > 0:
        chunk = sock.recv(min(_CHUNK_READ, remaining))
        if not chunk:
            break
        data.extend(chunk)
        remaining -= len(chunk)
        _check_cap(len(data), max_bytes)
    return bytes(data)


def _read_until_close(sock, already: bytes, max_bytes: int) -> bytes:
    data = bytearray(already)
    _check_cap(len(data), max_bytes)
    while True:
        chunk = sock.recv(_CHUNK_READ)
        if not chunk:
            break
        data.extend(chunk)
        _check_cap(len(data), max_bytes)
    return bytes(data)


def _read_chunked(sock, already: bytes, max_bytes: int) -> bytes:
    buffer = bytearray(already)
    out = bytearray()
    while True:
        while b"\r\n" not in buffer:
            if len(buffer) > _MAX_HEADER_BYTES:
                raise ExecutionError(ExecutionErrorCode.EGRESS_DENIED, "malformed chunk header")
            chunk = sock.recv(_CHUNK_READ)
            if not chunk:
                raise ExecutionError(ExecutionErrorCode.EGRESS_DENIED, "connection closed mid-chunk")
            buffer.extend(chunk)
        line, _, remainder = bytes(buffer).partition(b"\r\n")
        buffer = bytearray(remainder)
        try:
            size = int(line.split(b";", 1)[0].strip(), 16)
        except ValueError as exc:
            raise ExecutionError(ExecutionErrorCode.EGRESS_DENIED, "malformed chunk size") from exc
        if size < 0:
            raise ExecutionError(ExecutionErrorCode.EGRESS_DENIED, "malformed chunk size")
        if size == 0:
            break
        # The declared size is checked *before* reading it: otherwise one
        # chunk header claiming terabytes makes the loop below buffer until
        # memory runs out, and the cap check after it never gets to run.
        _check_cap(len(out) + size, max_bytes)
        while len(buffer) < size + 2:
            chunk = sock.recv(_CHUNK_READ)
            if not chunk:
                raise ExecutionError(ExecutionErrorCode.EGRESS_DENIED, "connection closed mid-chunk")
            buffer.extend(chunk)
        out.extend(buffer[:size])
        buffer = buffer[size + 2 :]
        _check_cap(len(out), max_bytes)
    return bytes(out)


def _check_cap(current: int, max_bytes: int) -> None:
    if current > max_bytes:
        raise ExecutionError(
            ExecutionErrorCode.RESPONSE_TOO_LARGE, f"response exceeds the {max_bytes}-byte cap"
        )


__all__ = ["EgressClient"]

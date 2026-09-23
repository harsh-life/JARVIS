"""Destination classification — 10 §4/§5's SSRF and DNS-rebinding defense.

The one property this module exists to make mechanically true: **a
destination check happens against a resolved IP address, not a hostname
string**, and it is applied *identically* however that IP was reached (a
direct connection, a redirect target, a rebound DNS answer) — there is no
code path here that trusts "the hostname looked fine."
"""

from __future__ import annotations

import ipaddress
from typing import Union

IPAddress = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]

# 10 §4: explicit, named cloud-metadata endpoints — blocked unconditionally,
# never overridable by `private_net` (unlike RFC1918/ULA ranges, which are).
# 169.254.169.254 is also covered by `is_link_local` below; it is restated
# here by name so the block is legible as "this is the metadata defense",
# not an incidental consequence of a broader rule, and so it still holds if
# `is_link_local` classification ever changes upstream.
_METADATA_ADDRESSES: frozenset[str] = frozenset(
    {
        "169.254.169.254",  # AWS / GCP / Azure IMDS
        "fd00:ec2::254",  # AWS IMDSv2 (IPv6)
        "::ffff:169.254.169.254",  # IPv4-mapped form of the above
    }
)


_SHARED_ADDRESS_SPACE = ipaddress.ip_network("100.64.0.0/10")


class DestinationBlocked(Exception):
    """Why an IP was refused — carried through to `ExecutionErrorCode.EGRESS_DENIED`."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _normalize(address: IPAddress) -> IPAddress:
    """Unwrap an IPv4-mapped IPv6 address (`::ffff:1.2.3.4`) to its IPv4
    form before classifying it — otherwise a metadata/private IPv4 address
    smuggled through its IPv6-mapped spelling would bypass every check
    below, which is exactly the "alternate IP representations" evasion this
    module has to close."""

    if isinstance(address, ipaddress.IPv6Address):
        mapped = address.ipv4_mapped
        if mapped is not None:
            return mapped
    return address


def classify(address_text: str, *, allow_private_net: bool) -> None:
    """Raise `DestinationBlocked` if `address_text` must never be connected
    to under the given policy. Never returns a "maybe" — a resolved address
    this module does not recognize as safe is never treated as safe by
    default (fail-closed): only an address that is neither metadata,
    loopback, unspecified, multicast/reserved, nor (unless explicitly
    allowed) private/link-local is permitted through.
    """

    try:
        parsed = _normalize(ipaddress.ip_address(address_text))
    except ValueError as exc:
        raise DestinationBlocked(f"not a valid IP address: {address_text!r}") from exc

    if str(parsed) in _METADATA_ADDRESSES or address_text in _METADATA_ADDRESSES:
        raise DestinationBlocked("cloud metadata endpoint — never reachable by a tool")
    if parsed.is_loopback:
        raise DestinationBlocked("loopback address — a tool cannot reach server-local services")
    if parsed.is_unspecified:
        raise DestinationBlocked("unspecified address")
    if parsed.is_multicast:
        raise DestinationBlocked("multicast address")
    if parsed.is_reserved:
        raise DestinationBlocked("reserved address range")
    if parsed.is_link_local:
        # Covers 169.254.0.0/16 (IPv4) and fe80::/10 (IPv6) — the metadata
        # IP is link-local too, so this alone would already catch it; the
        # explicit name-based check above stays as the legible, named
        # defense per 10 §4.
        raise DestinationBlocked("link-local address")
    if parsed.is_private and not allow_private_net:
        raise DestinationBlocked(
            "private address range — requires an execution policy with private_net=True"
        )
    if not parsed.is_global and not parsed.is_private:
        # Neither public nor RFC1918/ULA: e.g. 100.64.0.0/10 (RFC 6598
        # shared/CGNAT space — carrier NAT, and Tailscale-style overlays).
        # It is not the internet, so `internet=True` must not reach it; it is
        # a private network in all but name, so only `private_net` may.
        if not (allow_private_net and parsed in _SHARED_ADDRESS_SPACE):
            raise DestinationBlocked(
                "not a globally routable address — requires private_net=True"
            )


__all__ = ["DestinationBlocked", "classify"]

"""RAUTH-004's read predicate — the one definition in the codebase.

It lives in its own module, importing nothing but `shared/schemas` and the
descriptor type, so that every place that must apply it can import *this*
function rather than re-deriving it: the authorization engine
(`server/graph/authorization.py`, which re-exports it) and memory hydration
(`server/memory/hydration.py`, which may not import the engine because the
engine's audit path reaches `server.secrets` — 12 §6's "memory/vault never
resolve secrets" contract).

§9 of the security-core scope: "Do not duplicate visibility logic across
multiple modules. Create ONE authoritative authorization/visibility predicate."
Moving it here keeps it one.
"""

from __future__ import annotations

import uuid

from server.graph.ports import ResourceDescriptor
from shared.schemas.enums import Visibility


def readable(
    *, user_id: uuid.UUID, resource: ResourceDescriptor, is_active_member_of_resource_graph: bool
) -> bool:
    """RAUTH-004's read predicate, verbatim and in one place.

        readable(user, resource) :=
            (resource.visibility == graph AND active_member(user, resource.graph_id))
            OR resource.owner_user_id == user

    `[LOCKED]` (RAUTH-002) "No other path to readability exists." In particular
    `graph_id` alone never authorizes: the membership flag passed in is only
    consulted when `visibility` is `graph`.

    Note the membership argument is about **`resource.graph_id`**, not about the
    graph the request nominated. Those differ whenever a request omits its graph
    context or names a different graph, and checking the request's graph here
    would let a member of graph X read a graph-visible resource scoped to graph
    Y. The caller resolves membership against the resource's own graph.
    """

    if resource.owner_user_id == user_id:
        return True
    if (
        resource.visibility is Visibility.GRAPH
        and resource.graph_id is not None
        and is_active_member_of_resource_graph
    ):
        return True
    return False

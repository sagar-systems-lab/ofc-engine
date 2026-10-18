from __future__ import annotations

from dataclasses import dataclass

from .models import instant


@dataclass(frozen=True)
class Delegation:
    tenant_id: str
    owner: str
    delegate: str
    plan_digest: str
    node_ids: frozenset[str]
    permissions: frozenset[str]
    expires_at: str
    revoked: bool = False

    def __post_init__(self):
        if (
            not self.node_ids
            or len(self.node_ids) > 64
            or not self.permissions
            or not self.permissions <= {"read", "patch", "approve", "propose"}
            or len(self.plan_digest) != 64
            or type(self.revoked) is not bool
        ):
            raise ValueError("invalid server delegation")
        instant(self.expires_at)

    def allows(self, *, tenant_id, owner, delegate, plan_digest, nodes, permission, at):
        return (
            not self.revoked
            and instant(at) < instant(self.expires_at)
            and (tenant_id, owner, delegate, plan_digest)
            == (self.tenant_id, self.owner, self.delegate, self.plan_digest)
            and set(nodes) <= self.node_ids
            and permission in self.permissions
        )


def authorized(
    grants, *, tenant_id, owner, delegate, plan_digest, nodes, permission, at
):
    return owner == delegate or any(
        g.allows(
            tenant_id=tenant_id,
            owner=owner,
            delegate=delegate,
            plan_digest=plan_digest,
            nodes=nodes,
            permission=permission,
            at=at,
        )
        for g in grants
    )


def review_actor(runtime, review_id, identity, nodes=None, permission="approve"):
    from .models import utc_now
    from .storage import NotFound

    store = runtime.store
    with store._tx() as db:
        row = store._run(
            db,
            f"select actor_id,current_digest from {store.prefix}reviews where review_id=? and tenant_id=?",
            (review_id, identity.tenant_id),
        ).fetchone()
    if row is None:
        raise NotFound("review unavailable")
    if nodes is None:
        request = store.request(review_id, identity.tenant_id, row[0])
        nodes = tuple(n.id for n in request.plan.nodes)
    if not authorized(
        getattr(runtime, "delegations", ()),
        tenant_id=identity.tenant_id,
        owner=row[0],
        delegate=identity.actor_id,
        plan_digest=row[1],
        nodes=nodes,
        permission=permission,
        at=utc_now(),
    ):
        raise NotFound("review unavailable")
    return row[0]

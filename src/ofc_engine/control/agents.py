from __future__ import annotations

from dataclasses import dataclass
from urllib.error import HTTPError

from .models import ReviewRequest


@dataclass(frozen=True)
class AgentFault:
    code: str
    retryable_read: bool
    status: int
    fields: tuple[str, ...] = ()

    def data(self):
        return {
            "code": self.code,
            "status": self.status,
            "retryable_read": self.retryable_read,
            "fields": list(self.fields),
            "write_retry_allowed": False,
        }


def fault(error):
    status = (
        getattr(error, "code", 0)
        if isinstance(error, HTTPError)
        else getattr(error, "status", 0)
    )
    code = {
        403: "ACCESS_DENIED",
        404: "RESOURCE_UNAVAILABLE",
        409: "REVISION_CONFLICT",
        422: "INVALID_INPUT",
        429: "CAPACITY_UNAVAILABLE",
        503: "TEMPORARILY_UNAVAILABLE",
    }.get(status, "TRANSPORT_UNAVAILABLE")
    return AgentFault(code, status in {429, 503} or status == 0, status)


class AgentTools:
    def __init__(self, client, *, tenant_id, actor_id):
        self.client, self.tenant_id, self.actor_id = client, tenant_id, actor_id

    def submit(self, payload):
        request = ReviewRequest.parse(payload)
        if (request.plan.tenant_id, request.plan.actor.id) != (
            self.tenant_id,
            self.actor_id,
        ):
            raise PermissionError("AGENT_IDENTITY_MISMATCH")
        return self.client.submit(request)

    def status(self, review_id):
        return self.client.status(review_id)

    def evidence(self, review_id):
        return self.client.proof(review_id)

    def invoke(self, name, payload):
        try:
            if name == "review_plan":
                result = self.submit(payload)
            elif name == "review_status":
                result = self.status(payload["review_id"])
            elif name == "review_evidence":
                result = self.evidence(payload["review_id"])
            else:
                raise ValueError("UNKNOWN_TOOL")
            return {"ok": True, "result": result, "external_action_executed": False}
        except (HTTPError, OSError, TimeoutError) as exc:
            return {
                "ok": False,
                "fault": fault(exc).data(),
                "external_action_executed": False,
            }

    @staticmethod
    def schema():
        return {
            "schema": "ofc.agent-tools/1",
            "tools": [
                {"name": "review_plan", "input": ReviewRequest.model_json_schema()},
                {
                    "name": "review_status",
                    "input": {
                        "type": "object",
                        "properties": {"review_id": {"type": "string"}},
                        "required": ["review_id"],
                        "additionalProperties": False,
                    },
                },
                {
                    "name": "review_evidence",
                    "input": {
                        "type": "object",
                        "properties": {"review_id": {"type": "string"}},
                        "required": ["review_id"],
                        "additionalProperties": False,
                    },
                },
            ],
        }

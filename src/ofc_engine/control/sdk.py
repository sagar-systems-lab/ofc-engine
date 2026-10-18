from __future__ import annotations

import json
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .canonical import canonical_bytes
from .models import ReviewRequest


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ControlClient:
    def __init__(self, base_url: str, token: str, *, timeout=5):
        parts = urlsplit(base_url)
        if (
            parts.scheme != "https"
            and not (
                parts.scheme == "http" and parts.hostname in {"127.0.0.1", "localhost"}
            )
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
            or parts.path not in ("", "/")
        ):
            raise ValueError("HTTPS origin required")
        self.origin, self.token, self.timeout = base_url.rstrip("/"), token, timeout

    def _request(self, path, payload=None):
        headers = {
            "Authorization": "Bearer " + self.token,
            "Accept": "application/json",
        }
        data = None if payload is None else canonical_bytes(payload)
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = Request(self.origin + path, data=data, headers=headers)
        with build_opener(_NoRedirect()).open(
            request, timeout=self.timeout
        ) as response:
            raw = response.read(8_000_001)
            if len(raw) > 8_000_000:
                raise ValueError("response exceeds size limit")
            return json.loads(raw)

    def submit(self, request: ReviewRequest):
        return self._request("/api/control/plans", request.data())

    def status(self, review_id):
        return self._request(self._path(review_id))

    def proof(self, review_id):
        return self._request(self._path(review_id) + "/proof")

    @staticmethod
    def _path(review_id):
        import re

        if not re.fullmatch(r"pr-[a-f0-9]{32}", review_id):
            raise ValueError("invalid review id")
        return "/api/control/reviews/" + review_id

    def events(self, review_id, *, after=0):
        if type(after) is not int or not 0 <= after <= 10000:
            raise ValueError("invalid event cursor")
        return self._request(self._path(review_id) + "/events?after=" + str(after))

    def propose(self, review_id, proposal):
        return self._request(self._path(review_id) + "/proposals", proposal)

    def cancel(self, review_id):
        return self._request(self._path(review_id) + "/cancel", {"confirm": "CANCEL"})

    def resume(self, review_id):
        return self._request(self._path(review_id) + "/resume", {"confirm": "RESUME"})

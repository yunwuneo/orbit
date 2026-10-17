"""Bounded OAuth JSON/form and SSE transport; never retain remote error messages."""

import json
import socket
import time
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request

from .transport import RemoteError, Transport


API = "https://api.openai.com/v1"
ISSUER = "https://auth.openai.com"
AUTHORIZE = ISSUER + "/api/accounts/authorize"
TOKEN = ISSUER + "/api/accounts/oauth/token"
DISCOVERY = ISSUER + "/.well-known/openid-configuration"
PLAN_SCOPE = "chatgpt.tokens.use.direct"
MAX_BYTES = 4_000_000
ERRORS = {
    "subscription_sharing_user_not_eligible", "subscription_sharing_usage_limit_exceeded",
    "subscription_sharing_usage_unavailable", "subscription_sharing_unsupported_capability",
    "subscription_sharing_route_not_supported", "subscription_sharing_invalid_user",
    "subscription_sharing_user_unavailable", "chatpass_v2_scope_not_authorized",
    "chatpass_v2_invalid_authorization_context", "invalid_grant", "invalid_refresh_token",
    "token_expired", "refresh_token_expired", "refresh_token_invalidated",
    "refresh_token_invalid", "refresh_token_reused", "invalid_client",
}
TERMINAL_REFRESH = {
    "invalid_grant", "invalid_refresh_token", "token_expired", "refresh_token_expired",
    "refresh_token_invalidated", "refresh_token_invalid", "refresh_token_reused",
}


def remote_error(service, status=None, payload=None):
    detail = payload.get("error", {}) if isinstance(payload, dict) else {}
    code = detail.get("code") if isinstance(detail, dict) else detail
    reason = code if isinstance(code, str) and code in ERRORS else None
    retryable = status in {408, 409, 429} or (status is not None and status >= 500)
    if reason in {"subscription_sharing_usage_limit_exceeded", "invalid_client"} | TERMINAL_REFRESH:
        retryable = False
    return RemoteError(service, status, retryable=retryable, reason=reason)


class ChatGPTTransport(Transport):
    def json(self, method, url, headers=None, body=None, form=False, empty=False):
        headers = dict(headers or {})
        data = None
        if body is not None:
            data = urlencode(body).encode() if form else json.dumps(body).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded" if form else "application/json"
        request = Request(url, data=data, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    raise RemoteError("ChatGPT", retryable=False, reason="response_too_large")
                return {} if empty and not raw else json.loads(raw)
        except HTTPError as exc:
            try:
                payload = json.loads(exc.read(65537))
            except (ValueError, OSError, HTTPException):
                payload = None
            finally:
                exc.close()
            raise remote_error("ChatGPT", exc.code, payload) from None
        except (URLError, TimeoutError, socket.timeout, OSError, ValueError, HTTPException):
            raise RemoteError("ChatGPT", reason="transport_error") from None

    def events(self, body, access_token):
        # Hard-coded origin prevents forwarding a ChatGPT credential to a custom provider.
        request = Request(API + "/responses", data=json.dumps(body).encode(), method="POST",
                          headers={"Authorization": "Bearer " + access_token,
                                   "Content-Type": "application/json", "Accept": "text/event-stream"})
        started = time.monotonic()
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                if response.headers.get_content_type() != "text/event-stream":
                    raise RemoteError("ChatGPT", retryable=False, reason="expected_event_stream")
                total, data = 0, []
                while True:
                    line = response.readline(1_000_001)
                    total += len(line)
                    if total > MAX_BYTES or len(line) > 1_000_000:
                        raise RemoteError("ChatGPT", retryable=False, reason="response_too_large")
                    if time.monotonic() - started > self.timeout:
                        raise RemoteError("ChatGPT", reason="stream_timeout")
                    if not line:
                        # An event without its terminating blank line is incomplete.
                        return
                    line = line.decode("utf-8").rstrip("\r\n")
                    if not line:
                        if data:
                            raw, data = "\n".join(data), []
                            if raw == "[DONE]":
                                return
                            event = json.loads(raw)
                            if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                                raise ValueError()
                            yield event
                    elif line.startswith("data:"):
                        data.append(line[5:].removeprefix(" "))
        except HTTPError as exc:
            try:
                payload = json.loads(exc.read(65537))
            except (ValueError, OSError, HTTPException):
                payload = None
            finally:
                exc.close()
            raise remote_error("ChatGPT", exc.code, payload) from None
        except (URLError, TimeoutError, socket.timeout, OSError, ValueError, HTTPException):
            raise RemoteError("ChatGPT", reason="stream_interrupted") from None

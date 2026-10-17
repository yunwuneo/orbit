"""Responses adapter for a ChatGPT plan. No tools or remote conversation storage."""

from contextlib import closing

from .chatgpt_auth import ChatGPTAuth
from .chatgpt_transport import API, ChatGPTTransport, remote_error
from .transport import RemoteError


class ChatGPT:
    def __init__(self, config, http=None, auth=None):
        self.config = config
        self.http = http or ChatGPTTransport(config.timeout_seconds)
        self.auth = auth or ChatGPTAuth(config.chatgpt_auth_file, self.http)

    def models(self):
        session = self.auth.session()
        result = self.http.json("GET", API + "/models", {"Authorization": "Bearer " + session["access_token"]})
        try:
            rows = [{"slug": r["slug"], "display_name": r["display_name"]}
                    for r in result["models"] if r.get("visibility") == "list"]
            for row in rows:
                if any(not isinstance(v, str) or len(v) > 256 or not v for v in row.values()):
                    raise ValueError()
                if any(secret in v for secret in self.secrets(session) for v in row.values()):
                    raise ValueError()
            return rows
        except (KeyError, TypeError, ValueError, AttributeError):
            raise RemoteError("ChatGPT", retryable=False, reason="model_catalog_invalid") from None

    @staticmethod
    def secrets(session):
        return tuple(session[n] for n in ["access_token", "refresh_token", "id_token"] if session.get(n))

    def complete(self, instructions, data):
        self.config.require_model()
        body = {"model": self.config.model_name, "instructions": instructions,
                "input": [{"role": "user", "content": data}], "store": False, "stream": True}
        for attempt in range(3):
            session = self.auth.session()
            try:
                result = self.read_stream(body, session["access_token"])
                return result, self.secrets(session)
            except RemoteError as exc:
                # Invalid credentials are not refreshed/retried blindly; expiry is handled before sending.
                if not exc.retryable or attempt == 2:
                    raise
                self.http.sleep(min(2 ** attempt, 8))

    def read_stream(self, body, token):
        chunks, length = [], 0
        with closing(self.http.events(body, token)) as stream:
            for event in stream:
                kind = event["type"]
                if kind == "response.output_text.delta":
                    delta = event.get("delta")
                    if not isinstance(delta, str):
                        raise RemoteError("ChatGPT", retryable=False, reason="invalid_stream")
                    length += len(delta)
                    if length > 100000:
                        raise RemoteError("ChatGPT", retryable=False, reason="response_too_large")
                    chunks.append(delta)
                elif kind in {"error", "response.failed", "response.incomplete"}:
                    response = event.get("response", {})
                    payload = response if kind != "error" else {"error": event.get("error", event)}
                    error = remote_error("ChatGPT", payload=payload)
                    if error.reason in {"subscription_sharing_usage_unavailable", "subscription_sharing_user_unavailable"}:
                        error.retryable = True
                    if error.reason is None:
                        error.reason = "response_incomplete" if kind == "response.incomplete" else "response_failed"
                    raise error
                elif kind == "response.completed":
                    try:
                        response = event["response"]
                        if response["status"] != "completed" or response.get("error"):
                            raise ValueError()
                        texts = []
                        for item in response["output"]:
                            if item["type"] == "reasoning":
                                continue
                            if (item["type"] != "message" or item.get("role") != "assistant"
                                    or item.get("status", "completed") != "completed"):
                                raise ValueError()
                            for content in item["content"]:
                                if content["type"] != "output_text" or not isinstance(content["text"], str):
                                    raise ValueError()
                                texts.append(content["text"])
                        text = "".join(texts)
                        if not text or len(text) > 100000 or (chunks and text != "".join(chunks)):
                            raise ValueError()
                        return text
                    except (KeyError, TypeError, ValueError):
                        raise RemoteError("ChatGPT", retryable=False, reason="invalid_completed_response") from None
        raise RemoteError("ChatGPT", reason="stream_interrupted")

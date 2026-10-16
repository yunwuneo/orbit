"""Bounded HTTPS JSON. Never expose server bodies, URLs, or credentials in errors."""

import json
import socket
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener


class RemoteError(RuntimeError):
    def __init__(self, service, status=None, retryable=True, reason=None):
        self.service = service
        self.reason = reason
        self.retryable = retryable
        self.status = status
        super().__init__(f"{service}: 请求失败" + (f" (HTTP {status})" if status else ""))


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Transport:
    def __init__(self, timeout=45, sleep=time.sleep):
        self.timeout, self.sleep = timeout, sleep
        self.opener = build_opener(NoRedirect())

    def request(self, service, method, url, headers, body=None, attempts=1):
        for attempt in range(attempts):
            request = Request(url, data=None if body is None else json.dumps(body).encode(),
                              headers={**headers, "Content-Type": "application/json"}, method=method)
            try:
                with self.opener.open(request, timeout=self.timeout) as response:
                    data = response.read(4_000_001)
                    if len(data) > 4_000_000:
                        raise RemoteError(service, retryable=False)
                    return json.loads(data)
            except HTTPError as exc:
                retryable = exc.code in {408, 409, 429} or exc.code >= 500
                error = RemoteError(service, exc.code, retryable)
                exc.close()
            except (URLError, TimeoutError, socket.timeout, ConnectionError, OSError, ValueError) as exc:
                cause = exc.reason if isinstance(exc, URLError) else exc
                error = RemoteError(service, reason=type(cause).__name__)
            if not error.retryable or attempt == attempts - 1:
                raise error from None
            self.sleep(min(2 ** attempt, 8))

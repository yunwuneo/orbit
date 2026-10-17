"""ChatGPT public-client OAuth. Credentials stay outside SQLite, prompts and Notion."""

import base64
import fcntl
import hashlib
import hmac
import json
import math
import os
import secrets
import stat
import tempfile
import time
from datetime import datetime
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import uuid4

from .chatgpt_transport import (API, AUTHORIZE, DISCOVERY, ISSUER, PLAN_SCOPE, TOKEN,
                                TERMINAL_REFRESH, ChatGPTTransport)
from .transport import RemoteError


class AuthError(RemoteError):
    def __init__(self, reason="auth_required", retryable=False):
        super().__init__("ChatGPT Auth", retryable=retryable, reason=reason)
        self.args = ("ChatGPT 认证不可用（" + reason + "）；请检查 chatgpt-status 或重新运行 chatgpt-login",)


def text(value):
    return isinstance(value, str) and 0 < len(value) <= 32000 and not any(ord(c) < 32 for c in value)


def secure_read(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or info.st_size > 262144):
                raise AuthError("credential_permissions")
            raw = stream.read(262145)
            if len(raw) > 262144:
                raise AuthError("credential_size")
            return json.loads(raw)
    except FileNotFoundError:
        raise
    except (OSError, ValueError, UnicodeError):
        raise AuthError("credential_file_invalid") from None


class Credentials:
    def __init__(self, path):
        self.path = Path(path).expanduser().absolute()

    @contextmanager
    def lock(self):
        directory = self.path.parent
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = directory.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022):
            raise AuthError("credential_directory_permissions")
        fd = os.open(str(self.path) + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise AuthError("credential_permissions")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise AuthError("credential_busy", retryable=True) from None
            yield
        finally:
            os.close(fd)

    def read(self):
        try:
            data = secure_read(self.path)
        except FileNotFoundError:
            return {"version": 1, "host_id": "urn:uuid:" + str(uuid4()), "active": None, "accounts": {}}
        if (not isinstance(data, dict) or data.get("version") != 1 or not text(data.get("host_id"))
                or not isinstance(data.get("accounts"), dict) or len(data["accounts"]) > 32
                or (data.get("active") is not None and (not text(data["active"]) or data["active"] not in data["accounts"]))
                or (data.get("pending_client_id") is not None and
                    (not text(data["pending_client_id"]) or data["pending_client_id"] == "dynamic_agent_client"))):
            raise AuthError("credential_file_invalid")
        for client_id, record in data["accounts"].items():
            if (not text(client_id) or client_id == "dynamic_agent_client" or not isinstance(record, dict)
                    or record.get("client_id") != client_id or not text(record.get("subject"))
                    or record.get("issuer") != ISSUER
                    or not isinstance(record.get("scopes"), list) or not all(text(s) for s in record["scopes"])
                    or any(not text(record[n]) for n in ["access_token", "refresh_token", "id_token"] if n in record)
                    or any(not isinstance(record.get(n), (int, float)) or isinstance(record[n], bool)
                           or not math.isfinite(record[n]) for n in ["expires_at", "earliest_refresh_at"])):
                raise AuthError("credential_file_invalid")
        return data

    def write(self, data):
        raw = json.dumps(data, ensure_ascii=False, allow_nan=False)
        if len(data["accounts"]) > 32 or len(raw.encode()) > 262144:
            raise AuthError("credential_size")
        # Verify an existing target before replacement; never follow credential-file symlinks.
        if self.path.exists() or self.path.is_symlink():
            secure_read(self.path)
        fd, temporary = tempfile.mkstemp(prefix=".credentials-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            directory_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def jwt_library():
    try:
        import jwt
        from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: F401
        return jwt
    except ImportError:
        raise AuthError("install_orbit_chatgpt_extra") from None


def auth_endpoint(url):
    parsed = urlsplit(url) if isinstance(url, str) else None
    if (parsed is None or parsed.scheme != "https" or parsed.netloc != "auth.openai.com"
            or parsed.query or parsed.fragment or not parsed.path.startswith("/")):
        raise AuthError("discovery_invalid")
    return url


class ChatGPTAuth:
    def __init__(self, path, http=None, clock=time.time):
        self.credentials = Credentials(path)
        self.http = http or ChatGPTTransport()
        self.clock = clock

    def discovery(self):
        doc = self.http.json("GET", DISCOVERY)
        if not isinstance(doc, dict) or doc.get("issuer") != ISSUER:
            raise AuthError("discovery_invalid")
        return doc

    def verify_identity(self, token, client_id, nonce=None, subject=None, allow_expired=False):
        jwt = jwt_library()
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") not in {"RS256", "ES256"} or header.get("crit") or not text(header.get("kid")):
                raise AuthError("id_token_invalid")
            jwks = self.http.json("GET", auth_endpoint(self.discovery().get("jwks_uri")))
            keys = [k for k in jwks["keys"] if k.get("kid") == header["kid"]
                    and k.get("use", "sig") == "sig" and k.get("alg", header["alg"]) == header["alg"]]
            if len(keys) != 1:
                raise AuthError("id_token_invalid")
            key = jwt.PyJWK.from_dict(keys[0], algorithm=header["alg"]).key
            claims = jwt.decode(token, key, algorithms=[header["alg"]], audience=client_id, issuer=ISSUER,
                                options={"require": ["iss", "aud", "sub", "exp", "iat"],
                                         "verify_exp": not allow_expired}, leeway=30)
            if (not text(claims.get("sub")) or claims.get("azp", client_id) != client_id
                    or (isinstance(claims["aud"], list) and len(claims["aud"]) > 1 and claims.get("azp") != client_id)
                    or (nonce is not None and not hmac.compare_digest(str(claims.get("nonce", "")), nonce))
                    or (subject is not None and claims["sub"] != subject)):
                raise AuthError("id_token_invalid")
            return claims["sub"]
        except (ValueError, TypeError, KeyError, AttributeError, jwt.PyJWTError):
            raise AuthError("id_token_invalid") from None

    def token_record(self, result, previous, client_id, nonce=None):
        if not isinstance(result, dict):
            raise AuthError("token_response_invalid")
        record = dict(previous or {})
        for name in ["access_token", "refresh_token"]:
            if not text(result.get(name)):
                raise AuthError("token_response_invalid")
            record[name] = result[name]
        expires = result.get("expires_in")
        if (not isinstance(expires, (int, float)) or isinstance(expires, bool)
                or not math.isfinite(expires) or not 0 < expires <= 86400
                or str(result.get("token_type", "")).lower() != "bearer"):
            raise AuthError("token_response_invalid")
        scope = result.get("scope")
        if scope is None and previous:
            scopes = previous.get("scopes", [])
        elif isinstance(scope, str):
            scopes = scope.split()
        else:
            raise AuthError("token_response_invalid")
        if not isinstance(scopes, list) or not all(text(s) for s in scopes):
            raise AuthError("token_response_invalid")
        identity = result.get("id_token")
        if nonce is not None or identity is not None:
            if not text(identity):
                raise AuthError("id_token_invalid")
            record["subject"] = self.verify_identity(identity, client_id, nonce, record.get("subject"))
            record["id_token"] = identity
        if not text(record.get("subject")):
            raise AuthError("id_token_invalid")
        earliest = result.get("earliest_refresh_at", 0)
        if earliest is None:
            earliest = 0
        if isinstance(earliest, str):
            try:
                parsed = datetime.fromisoformat(earliest.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    raise ValueError()
                earliest = parsed.timestamp()
            except ValueError:
                raise AuthError("token_response_invalid") from None
        if not isinstance(earliest, (int, float)) or isinstance(earliest, bool) or not math.isfinite(earliest):
            raise AuthError("token_response_invalid")
        record.update(client_id=client_id, scopes=scopes, expires_at=self.clock() + expires,
                      earliest_refresh_at=earliest, issuer=ISSUER)
        return record

    def session(self):
        with self.credentials.lock():
            data = self.credentials.read()
            record = data["accounts"].get(data["active"])
            if not record or not text(record.get("access_token")) or not text(record.get("refresh_token")):
                raise AuthError()
            expires = record.get("expires_at")
            earliest = record.get("earliest_refresh_at", 0)
            if (not isinstance(expires, (int, float)) or isinstance(expires, bool) or not math.isfinite(expires)
                    or not isinstance(earliest, (int, float)) or isinstance(earliest, bool) or not math.isfinite(earliest)
                    or not isinstance(record.get("scopes"), list) or not all(text(s) for s in record["scopes"])):
                raise AuthError("credential_file_invalid")
            if expires <= self.clock() + 60 and earliest <= self.clock():
                try:
                    response = self.http.json("POST", TOKEN, body={"grant_type": "refresh_token",
                                              "client_id": record["client_id"], "refresh_token": record["refresh_token"],
                                              "resource": API}, form=True)
                except RemoteError as exc:
                    if exc.reason in TERMINAL_REFRESH:
                        for name in ["access_token", "refresh_token", "id_token"]:
                            record.pop(name, None)
                        self.credentials.write(data)
                        raise AuthError("session_expired") from None
                    raise
                # A successful refresh rotates credentials, including a revoked plan scope.
                record = self.token_record(response, record, record["client_id"])
                data["accounts"][data["active"]] = record
                self.credentials.write(data)
            if expires <= self.clock() and earliest > self.clock():
                raise AuthError("refresh_not_ready", retryable=True)
            if PLAN_SCOPE not in record.get("scopes", []):
                raise AuthError("plan_permission_missing")
            return dict(record)

    def status(self):
        # Read-only: no directory creation, token refresh, model request, or database.
        data = self.credentials.read()
        return {"provider": "chatgpt", "accounts": [
            {"client_id": key, "active": key == data["active"],
             "signed_in": bool(r.get("refresh_token") and r.get("access_token")),
             "plan_enabled": PLAN_SCOPE in r.get("scopes", []), "expires_at": r.get("expires_at")}
            for key, r in data["accounts"].items()]}

    def select(self, client_id):
        with self.credentials.lock():
            data = self.credentials.read()
            if client_id not in data["accounts"]:
                raise AuthError("unknown_account")
            data["active"] = client_id
            self.credentials.write(data)

    def logout(self):
        with self.credentials.lock():
            data = self.credentials.read()
            record = data["accounts"].get(data["active"])
            if not record:
                return {"signed_out": True, "revocation_confirmed": True}
            confirmed = not bool(record.get("refresh_token"))
            if not confirmed:
                try:
                    endpoint = auth_endpoint(self.discovery().get("revocation_endpoint"))
                    self.http.json("POST", endpoint, body={"token": record["refresh_token"],
                                   "token_type_hint": "refresh_token", "client_id": record["client_id"]}, form=True, empty=True)
                    confirmed = True
                except RemoteError:
                    pass
            for name in ["access_token", "refresh_token", "id_token"]:
                record.pop(name, None)
            self.credentials.write(data)
            return {"signed_out": True, "revocation_confirmed": confirmed}

    def import_file(self, source):
        # Transfer only the selected registration, never overwrite this runtime's host ID.
        imported = Credentials(source).read()
        record = imported["accounts"].get(imported["active"])
        if not record or not text(record.get("id_token")):
            raise AuthError("import_invalid")
        self.verify_identity(record["id_token"], record["client_id"], subject=record["subject"], allow_expired=True)
        if not all(text(record.get(n)) for n in ["access_token", "refresh_token"]):
            raise AuthError("import_invalid")
        with self.credentials.lock():
            data = self.credentials.read()
            old = data["accounts"].get(record["client_id"])
            if old and old["subject"] != record["subject"]:
                raise AuthError("account_mismatch")
            data["accounts"][record["client_id"]] = dict(record)
            data["active"] = record["client_id"]
            self.credentials.write(data)

    def login(self, port=1455, timeout=180, new_account=False, announce=print):
        jwt_library()  # Fail before registration if signature verification is unavailable.
        if type(port) is not int or not 0 <= port <= 65535 or not 1 <= timeout <= 600:
            raise AuthError("login_arguments")
        with self.credentials.lock():
            data = self.credentials.read()
            self.credentials.write(data)  # Persist the host identity before first authorization.
            previous = None if new_account else data["accounts"].get(data["active"])
            previous = dict(previous) if previous else None
            host_id = data["host_id"]
            saved_client = previous["client_id"] if previous else data.get("pending_client_id")
        state, nonce, verifier = (secrets.token_urlsafe(48) for _ in range(3))
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        pending = {}

        class Callback(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass  # Never log callback URLs, codes or browser headers.

            def do_GET(self):
                try:
                    url = urlsplit(self.path)
                    params = parse_qs(url.query, strict_parsing=True, max_num_fields=20)
                    valid = (len(self.path) <= 16384 and url.path == "/auth/callback"
                             and self.headers.get("Host") == "127.0.0.1:" + str(self.server.server_port)
                             and all(len(v) == 1 for v in params.values())
                             and hmac.compare_digest(params.get("state", [""])[0], state))
                except (ValueError, TypeError):
                    valid = False
                self.send_response(200 if valid else 400)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.end_headers()
                self.wfile.write(b"Authorization received. Check the Orbit terminal." if valid else b"Invalid callback.")
                if valid:
                    pending.update({key: value[0] for key, value in params.items()})

        class LoopbackServer(HTTPServer):
            def get_request(self):
                connection, address = super().get_request()
                connection.settimeout(2)
                return connection, address

            def handle_error(self, request, client_address):
                pass  # No traceback containing callback data on a broken local connection.

        with LoopbackServer(("127.0.0.1", port), Callback) as server:
            server.timeout = 1
            redirect = "http://127.0.0.1:" + str(server.server_port) + "/auth/callback"
            params = {"client_id": saved_client or "dynamic_agent_client",
                      "ext_agent_host_id": host_id, "response_type": "code", "redirect_uri": redirect,
                      "scope": "openid profile email offline_access resource.invoke " + PLAN_SCOPE,
                      "resource": API, "state": state, "nonce": nonce,
                      "code_challenge_method": "S256", "code_challenge": challenge}
            if not saved_client:
                params["agent_name_hint"] = "Orbit"
            # No retained ID token in the printed URL. Browser opening is a user action.
            announce("Continue with ChatGPT：请在本机浏览器打开以下链接\n" + AUTHORIZE + "?" + urlencode(params))
            deadline = time.monotonic() + timeout
            while not pending and time.monotonic() < deadline:
                server.handle_request()
        if not pending:
            raise AuthError("login_timeout")
        if "error" in pending or not text(pending.get("code")):
            raise AuthError("authorization_declined")
        client_id = pending.get("client_id", saved_client)
        if (not text(client_id) or client_id == "dynamic_agent_client"
                or (saved_client and client_id != saved_client)):
            raise AuthError("client_mismatch")
        with self.credentials.lock():
            data = self.credentials.read()
            if not previous:
                # Retain the issued registration even if this one-time code cannot be redeemed.
                data["pending_client_id"] = client_id
                self.credentials.write(data)
            result = self.http.json("POST", TOKEN, body={"grant_type": "authorization_code", "code": pending["code"],
                                    "client_id": client_id, "code_verifier": verifier,
                                    "redirect_uri": redirect, "resource": API}, form=True)
            record = self.token_record(result, previous, client_id, nonce)
            old = data["accounts"].get(client_id)
            if old and old["subject"] != record["subject"]:
                raise AuthError("account_mismatch")
            data["accounts"][client_id] = record
            data["active"] = client_id
            if data.get("pending_client_id") == client_id:
                data.pop("pending_client_id")
            self.credentials.write(data)
        return self.status()

"""Offline OAuth/Responses tests. All credentials and signatures are synthetic."""

import base64
import copy
import io
import json
import os
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from email.message import Message
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit

from orbit.chatgpt import ChatGPT
from orbit.chatgpt_auth import AuthError, ChatGPTAuth, Credentials
from orbit.chatgpt_transport import API, DISCOVERY, ISSUER, PLAN_SCOPE, TOKEN, ChatGPTTransport
from orbit.cli import main
from orbit.config import Config, ConfigError, load_config
from orbit.model import InvalidOutput, Model
from orbit.transport import RemoteError

try:
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa
except ImportError:
    jwt = None


def completed(raw='{"text":"ok"}', **changes):
    response = {"status": "completed", "output": [{"type": "message", "role": "assistant",
                "status": "completed", "content": [{"type": "output_text", "text": raw}]}]}
    response.update(changes)
    return {"type": "response.completed", "response": response}


class HTTP:
    def __init__(self):
        self.calls, self.bodies, self.sleeps = [], [], []
        self.events_list = [{"type": "response.output_text.delta", "delta": '{"text":"ok"}'}, completed()]
        self.refreshed = {"access_token": "fresh-access-secret", "refresh_token": "fresh-refresh-secret",
                          "expires_in": 3600, "token_type": "Bearer", "scope": PLAN_SCOPE}
        self.failure = None
        self.closed = 0

    def sleep(self, seconds):
        self.sleeps.append(seconds)

    def json(self, method, url, headers=None, body=None, form=False, empty=False):
        self.calls.append((method, url, headers, body, form))
        if self.failure:
            raise self.failure
        if url == TOKEN:
            return copy.deepcopy(self.refreshed)
        if url == API + "/models":
            return {"models": [{"slug": "available", "display_name": "Available", "visibility": "list"},
                               {"slug": "hidden", "visibility": "hide"}]}
        if url == DISCOVERY:
            return {"issuer": ISSUER, "jwks_uri": ISSUER + "/.well-known/jwks.json",
                    "revocation_endpoint": ISSUER + "/revoke"}
        if url.endswith("jwks.json"):
            return {"keys": [self.jwk]}
        if url.endswith("/revoke"):
            return {}
        raise AssertionError("unexpected URL")

    def events(self, body, token):
        self.bodies.append(copy.deepcopy(body))
        try:
            for item in self.events_list:
                if isinstance(item, Exception):
                    raise item
                yield copy.deepcopy(item)
        finally:
            self.closed += 1


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="orbit-chatgpt-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "auth" / "credentials.json"
        self.config = Config(model_provider="chatgpt", model_name="chosen-model",
                             chatgpt_auth_file=str(self.path), notion_token="notion-secret")
        self.http = HTTP()
        self.auth = ChatGPTAuth(self.path, self.http, clock=lambda: 1000)
        self.record = {"client_id": "oaiapp_fixture", "subject": "fixture-subject", "issuer": ISSUER,
                       "access_token": "access-secret", "refresh_token": "refresh-secret", "id_token": "identity-secret",
                       "expires_at": 4600, "scopes": [PLAN_SCOPE], "earliest_refresh_at": 0}
        self.save(self.record)

    def save(self, record):
        with self.auth.credentials.lock():
            data = self.auth.credentials.read()
            data["accounts"][record["client_id"]] = copy.deepcopy(record)
            data["active"] = record["client_id"]
            self.auth.credentials.write(data)

    def adapter(self):
        return ChatGPT(self.config, self.http, self.auth)

    def model(self):
        model = Model(self.config)
        model.chatgpt = self.adapter()
        return model


class ResponsesTests(Fixture):
    def test_model_json_and_original_contract(self):
        self.assertEqual(self.model().diary({"events": []}), "ok")
        body = self.http.bodies[0]
        self.assertEqual(set(body), {"model", "instructions", "input", "store", "stream"})
        self.assertEqual(body["model"], "chosen-model")
        self.assertFalse(body["store"])
        self.assertTrue(body["stream"])
        self.assertEqual(body["input"][0]["role"], "user")
        self.assertEqual(self.http.closed, 1)

    def test_no_api_key_or_custom_base_required(self):
        replace(self.config, model_base_url="https://third-party.invalid").require_connections()
        self.config.require_model()
        self.assertEqual(self.adapter().models(), [{"slug": "available", "display_name": "Available"}])
        self.assertEqual(self.http.calls[0][1], API + "/models")

    def test_json_without_deltas_requires_completed_event(self):
        self.http.events_list = [completed()]
        self.assertEqual(self.model().diary({}), "ok")

    def test_partial_output_never_succeeds_and_retries_are_bounded(self):
        self.http.events_list = [{"type": "response.output_text.delta", "delta": '{"text":"ok"}'}]
        with self.assertRaises(RemoteError) as error:
            self.model().diary({})
        self.assertEqual(error.exception.reason, "stream_interrupted")
        self.assertEqual(len(self.http.bodies), 3)
        self.assertEqual(self.http.closed, 3)
        self.assertEqual(self.http.sleeps, [1, 2])

    def test_quota_after_delta_does_not_retry_or_accept_partial_json(self):
        self.http.events_list[-1] = {"type": "response.failed", "response": {"error": {
            "code": "subscription_sharing_usage_limit_exceeded", "message": "private-remote-message"}}}
        with self.assertRaises(RemoteError) as error:
            self.model().diary({})
        self.assertFalse(error.exception.retryable)
        self.assertEqual(error.exception.reason, "subscription_sharing_usage_limit_exceeded")
        self.assertNotIn("private-remote-message", str(error.exception))
        self.assertEqual(len(self.http.bodies), 1)

    def test_incomplete_refused_mismatched_and_malformed_outputs_fail(self):
        for event in [
            {"type": "response.incomplete", "response": {}},
            completed(status="incomplete"),
            completed('{"text":"different"}'),
            completed(output=[{"type": "function_call"}]),
            completed(output=[{"type": "message", "role": "assistant", "content": [{"type": "refusal"}]}]),
            completed(output=[]),
        ]:
            with self.subTest(event=event):
                self.http.events_list = [{"type": "response.output_text.delta", "delta": '{"text":"ok"}'}, event]
                with self.assertRaises(RemoteError):
                    self.model().diary({})

    def test_secret_echo_and_non_json_fail_before_model_boundary(self):
        for raw in ['{"text":"' + secret + '"}' for secret in
                    ["access-secret", "refresh-secret", "identity-secret", "notion-secret"]] + ["not-json"]:
            self.http.events_list = [completed(raw)]
            with self.assertRaises(InvalidOutput) as error:
                self.model().diary({})
            self.assertNotIn(raw, str(error.exception))

    def test_output_size_limit(self):
        self.http.events_list = [{"type": "response.output_text.delta", "delta": "x" * 100001}]
        with self.assertRaises(RemoteError) as error:
            self.adapter().complete("system", "data")
        self.assertFalse(error.exception.retryable)

    def test_json_escaped_secret_is_rejected_after_decoding(self):
        self.http.events_list = [completed('{"text":"\\u0061ccess-secret"}')]
        with self.assertRaises(InvalidOutput):
            self.model().diary({})

    def test_stream_failure_produces_no_life_commit(self):
        from tests.support import Clock, Rest
        from orbit.engine import Engine
        from orbit.notion import Notion
        from orbit.store import Store
        clock = Clock()
        store = Store(str(Path(self.temp.name) / "test.sqlite3"))
        self.addCleanup(store.close)
        rest = Rest(self.config)
        self.http.events_list = [{"type": "response.failed", "response": {"error": {
            "code": "subscription_sharing_usage_limit_exceeded"}}}]
        engine = Engine(self.config, store, Notion(self.config, rest), self.model(), clock)
        self.assertEqual(engine.tick(), "generation_failed")
        self.assertEqual(store.db.execute("SELECT count(*) FROM runs WHERE mode!='operations'").fetchone()[0], 0)
        self.assertTrue(store.get("generation_blocked"))
        self.assertEqual(store.get("last_error")["reason"], "subscription_sharing_usage_limit_exceeded")
        self.assertEqual(rest.post_count, 0)


class RefreshTests(Fixture):
    def test_refresh_rotates_atomically_and_next_instance_reuses(self):
        self.save({**self.record, "expires_at": 1001})
        record = self.auth.session()
        self.assertEqual(record["access_token"], "fresh-access-secret")
        self.assertEqual(record["refresh_token"], "fresh-refresh-secret")
        self.assertEqual(record["expires_at"], 4600)
        _, url, _, body, form = self.http.calls[0]
        self.assertEqual(url, TOKEN)
        self.assertTrue(form)
        self.assertEqual(body, {"grant_type": "refresh_token", "client_id": "oaiapp_fixture",
                                "refresh_token": "refresh-secret", "resource": API})
        self.assertEqual(ChatGPTAuth(self.path, self.http, clock=lambda: 1001).session()["access_token"], "fresh-access-secret")
        self.assertEqual(len(self.http.calls), 1)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_refresh_omitted_scope_keeps_grant(self):
        self.save({**self.record, "expires_at": 1001})
        self.http.refreshed.pop("scope")
        self.assertIn(PLAN_SCOPE, self.auth.session()["scopes"])

    def test_refresh_scope_removed_is_saved_without_inference(self):
        self.save({**self.record, "expires_at": 1001})
        self.http.refreshed["scope"] = "openid"
        with self.assertRaises(AuthError):
            self.adapter().complete("system", "data")
        saved = self.auth.credentials.read()["accounts"]["oaiapp_fixture"]
        self.assertEqual(saved["refresh_token"], "fresh-refresh-secret")
        self.assertEqual(self.http.bodies, [])

    def test_terminal_refresh_clears_tokens_but_preserves_registration(self):
        self.save({**self.record, "expires_at": 1001})
        self.http.failure = RemoteError("ChatGPT", 400, False, reason="refresh_token_reused")
        with self.assertRaises(AuthError):
            self.auth.session()
        record = self.auth.credentials.read()["accounts"]["oaiapp_fixture"]
        self.assertNotIn("refresh_token", record)
        self.assertNotIn("id_token", record)
        self.assertEqual(record["subject"], "fixture-subject")

    def test_transient_refresh_keeps_credentials_and_does_not_retry_rotation(self):
        self.save({**self.record, "expires_at": 1001})
        before = self.path.read_bytes()
        self.http.failure = RemoteError("ChatGPT", 503)
        with self.assertRaises(RemoteError):
            self.auth.session()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(len(self.http.calls), 1)

    def test_refresh_lock_prevents_other_process_racing(self):
        with self.auth.credentials.lock():
            with self.assertRaises(AuthError) as error:
                ChatGPTAuth(self.path, self.http).session()
        self.assertEqual(error.exception.reason, "credential_busy")

    def test_earliest_refresh_and_invalid_responses(self):
        self.save({**self.record, "expires_at": 1030, "earliest_refresh_at": 1020})
        self.assertEqual(self.auth.session()["access_token"], "access-secret")
        self.assertEqual(self.http.calls, [])
        self.save({**self.record, "expires_at": 900, "earliest_refresh_at": 1020})
        with self.assertRaises(AuthError):
            self.auth.session()
        for change in [{"expires_in": float("nan")}, {"expires_in": True}, {"refresh_token": ""}, {"token_type": "other"}]:
            with self.subTest(change=change):
                with self.assertRaises(AuthError):
                    self.auth.token_record({**self.http.refreshed, **change}, self.record, self.record["client_id"])

    def test_status_is_offline_and_never_prints_secrets(self):
        self.http.failure = AssertionError("network forbidden")
        raw = json.dumps(self.auth.status())
        for secret in ["access-secret", "refresh-secret", "identity-secret", "fixture-subject"]:
            self.assertNotIn(secret, raw)
        self.assertEqual(self.http.calls, [])
        absent = Path(self.temp.name) / "never-created" / "credentials.json"
        self.assertEqual(ChatGPTAuth(absent).status()["accounts"], [])
        self.assertFalse(absent.parent.exists())

    def test_logout_revokes_and_preserves_host_and_registration(self):
        host = self.auth.credentials.read()["host_id"]
        self.assertTrue(self.auth.logout()["revocation_confirmed"])
        data = self.auth.credentials.read()
        self.assertEqual(data["host_id"], host)
        self.assertEqual(data["active"], "oaiapp_fixture")
        self.assertNotIn("access_token", data["accounts"][data["active"]])
        self.assertEqual(self.http.calls[-1][3]["token_type_hint"], "refresh_token")

    def test_unconfirmed_logout_and_account_selection(self):
        self.http.failure = RemoteError("ChatGPT", 503)
        self.assertFalse(self.auth.logout()["revocation_confirmed"])
        self.save({**self.record, "client_id": "oaiapp_second", "subject": "second-subject"})
        self.auth.select("oaiapp_fixture")
        self.assertEqual(len(self.auth.status()["accounts"]), 2)
        with self.assertRaises(AuthError):
            self.auth.select("missing")

    def test_symlink_and_world_readable_files_rejected(self):
        self.path.chmod(0o644)
        with self.assertRaises(AuthError):
            self.auth.session()
        self.path.chmod(0o600)
        link = self.path.with_name("link.json")
        link.symlink_to(self.path)
        with self.assertRaises(AuthError):
            Credentials(link).read()

    def test_failed_atomic_replace_keeps_original_and_removes_temporary(self):
        before = self.path.read_bytes()
        with self.auth.credentials.lock(), patch("orbit.chatgpt_auth.os.replace", side_effect=OSError()):
            data = self.auth.credentials.read()
            data["accounts"][data["active"]]["access_token"] = "replacement"
            with self.assertRaises(OSError):
                self.auth.credentials.write(data)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.glob(".credentials-*")), [])


@unittest.skipUnless(jwt, "install .[chatgpt] for actual JWT signature tests")
class IdentityTests(Fixture):
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(cls.key.public_key()))
        cls.jwk.update(kid="fixture-key", use="sig", alg="RS256")

    def setUp(self):
        super().setUp()
        self.http.jwk = self.jwk

    def identity(self, **changes):
        claims = {"iss": ISSUER, "aud": "oaiapp_fixture", "sub": "fixture-subject", "nonce": "fixture-nonce",
                  "iat": int(time.time()) - 1, "exp": int(time.time()) + 3600}
        claims.update(changes)
        return jwt.encode(claims, self.key, algorithm="RS256", headers={"kid": "fixture-key"})

    def test_signature_issuer_audience_nonce_and_subject_are_verified(self):
        token = self.identity()
        self.assertEqual(self.auth.verify_identity(token, "oaiapp_fixture", "fixture-nonce"), "fixture-subject")
        for changes in [{"iss": "https://evil.invalid"}, {"aud": "other-client"}, {"nonce": "wrong"},
                        {"exp": int(time.time()) - 120}, {"sub": "other-subject"},
                        {"aud": ["oaiapp_fixture", "other"], "azp": "other"}]:
            with self.subTest(changes=changes), self.assertRaises(AuthError):
                self.auth.verify_identity(self.identity(**changes), "oaiapp_fixture", "fixture-nonce", "fixture-subject")
        parts = token.split(".")
        parts[1] = base64.urlsafe_b64encode(json.dumps({"iss": ISSUER, "aud": "oaiapp_fixture", "sub": "forged"}).encode()).rstrip(b"=").decode()
        with self.assertRaises(AuthError):
            self.auth.verify_identity(".".join(parts), "oaiapp_fixture")

    def test_unsigned_and_symmetric_tokens_are_rejected(self):
        for algorithm, key in [("none", None), ("HS256", "fake-key" * 8)]:
            token = jwt.encode({"sub": "forged"}, key, algorithm=algorithm, headers={"kid": "fixture-key"})
            with self.assertRaises(AuthError):
                self.auth.verify_identity(token, "oaiapp_fixture")

    def login_callback(self, mutation=None, new_account=False, invalid_first=False):
        fixture = self
        announced = []

        class Server:
            def __init__(self, address, handler):
                fixture.assertEqual(address[0], "127.0.0.1")
                self.server_port, self.handler = 54321, handler

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def handle_request(self):
                params = parse_qs(urlsplit(announced[-1].splitlines()[-1]).query)
                fixture.auth_params = params
                fixture.http.refreshed["id_token"] = fixture.identity(nonce=params["nonce"][0])
                callback = {"code": "fixture-code", "state": params["state"][0], "client_id": "oaiapp_fixture"}
                if mutation:
                    mutation(callback)
                handler = object.__new__(self.handler)
                handler.path = "/auth/callback?" + urlencode(callback)
                handler.headers = {"Host": "127.0.0.1:54321"}
                handler.server, handler.wfile = self, io.BytesIO()
                handler.send_response = lambda status: fixture.assertEqual(status, 200)
                handler.send_header = lambda *args: None
                handler.end_headers = lambda: None
                if invalid_first:
                    # Foreign state, duplicate state, wrong callback path and Host never consume a code.
                    good_path = handler.path
                    for bad in ["/auth/callback?state=foreign", good_path + "&state=duplicate", "/wrong?" + urlencode(callback)]:
                        handler.path = bad
                        handler.send_response = lambda status: fixture.assertEqual(status, 400)
                        handler.do_GET()
                    handler.path = good_path
                    handler.headers = {"Host": "foreign.invalid"}
                    handler.do_GET()
                    handler.headers = {"Host": "127.0.0.1:54321"}
                    handler.send_response = lambda status: fixture.assertEqual(status, 200)
                handler.do_GET()

        with patch("orbit.chatgpt_auth.HTTPServer", Server):
            return self.auth.login(port=0, timeout=1, new_account=new_account, announce=announced.append)

    def test_loopback_registration_pkce_and_account_reauthorization(self):
        self.login_callback(new_account=True)
        self.assertEqual(self.auth_params["client_id"], ["dynamic_agent_client"])
        self.assertEqual(self.auth_params["agent_name_hint"], ["Orbit"])
        call = next(c for c in self.http.calls if c[1] == TOKEN)
        self.assertEqual(call[3]["redirect_uri"], self.auth_params["redirect_uri"][0])
        verifier = call[3]["code_verifier"]
        import hashlib
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        self.assertEqual(self.auth_params["code_challenge"], [challenge])
        self.assertEqual(call[3]["client_id"], "oaiapp_fixture")
        self.http.calls.clear()
        self.login_callback()
        self.assertEqual(self.auth_params["client_id"], ["oaiapp_fixture"])
        self.assertNotIn("agent_name_hint", self.auth_params)
        self.assertNotIn("id_token_hint", self.auth_params)

    def test_bad_client_callback_and_declined_consent_preserve_active_account(self):
        before = self.path.read_bytes()
        for mutate in [lambda c: c.update(client_id="other-client"), lambda c: c.update(error="access_denied")]:
            with self.assertRaises(AuthError):
                self.login_callback(mutate)
            self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(any(c[1] == TOKEN for c in self.http.calls))

    def test_callback_rejects_foreign_state_duplicates_path_and_host(self):
        self.login_callback(invalid_first=True)
        self.assertEqual(sum(c[1] == TOKEN for c in self.http.calls), 1)

    def test_failed_new_code_exchange_retains_issued_registration_for_next_login(self):
        self.path.unlink()
        self.http.failure = RemoteError("ChatGPT", 400, False, reason="invalid_grant")
        with self.assertRaises(RemoteError):
            self.login_callback(new_account=True)
        data = self.auth.credentials.read()
        self.assertEqual(data["pending_client_id"], "oaiapp_fixture")
        self.assertEqual(data["accounts"], {})
        self.http.failure = None
        self.login_callback()
        self.assertEqual(self.auth_params["client_id"], ["oaiapp_fixture"])
        self.assertNotIn("pending_client_id", self.auth.credentials.read())

    def test_import_validates_signature_and_preserves_vm_host(self):
        source = Path(self.temp.name) / "laptop" / "credentials.json"
        auth = ChatGPTAuth(source, self.http)
        with auth.credentials.lock():
            data = auth.credentials.read()
            data["accounts"]["oaiapp_fixture"] = {**self.record, "id_token": self.identity(exp=int(time.time()) - 120)}
            data["active"] = "oaiapp_fixture"
            auth.credentials.write(data)
        host = self.auth.credentials.read()["host_id"]
        self.auth.import_file(source)
        self.assertEqual(self.auth.credentials.read()["host_id"], host)
        self.assertNotEqual(host, auth.credentials.read()["host_id"])


class TransportTests(unittest.TestCase):
    def transport(self, content, content_type="text/event-stream"):
        class Response(io.BytesIO):
            headers = Message()
        response = Response(content)
        response.headers["Content-Type"] = content_type

        class Opener:
            def open(self, request, timeout):
                self.request = request
                return response
        http = ChatGPTTransport()
        http.opener = Opener()
        return http

    def test_sse_multiline_heartbeats_unicode_and_crlf(self):
        content = ': heartbeat\r\nevent: test\r\ndata: {"type": "response.output_text.delta",\r\ndata: "delta": "你好"}\r\n\r\ndata: [DONE]\r\n\r\n'.encode()
        http = self.transport(content)
        self.assertEqual(list(http.events({}, "fake-secret")), [{"type": "response.output_text.delta", "delta": "你好"}])
        self.assertEqual(http.opener.request.full_url, API + "/responses")

    def test_wrong_content_type_and_oversized_sse_are_rejected(self):
        for http in [self.transport(b"{}", "application/json"), self.transport(b"x" * 1_000_001)]:
            with self.assertRaises(RemoteError) as error:
                list(http.events({}, "fake-secret"))
            self.assertFalse(error.exception.retryable)

    def test_unterminated_event_is_not_delivered(self):
        http = self.transport(b'data: {"type":"response.completed"}\n')
        self.assertEqual(list(http.events({}, "fake-secret")), [])

    def test_form_encoding_and_empty_revocation_response(self):
        http = self.transport(b"", "application/json")
        self.assertEqual(http.json("POST", TOKEN, body={"token": "a+b/c"}, form=True, empty=True), {})
        self.assertIn(b"token=a%2Bb%2Fc", http.opener.request.data)
        self.assertEqual(http.opener.request.get_header("Content-type"), "application/x-www-form-urlencoded")

    def test_http_errors_are_classified_without_remote_text(self):
        class Opener:
            def open(self, *args, **kwargs):
                raise HTTPError(API, 429, "private-message", {}, io.BytesIO(
                    b'{"error":{"code":"subscription_sharing_usage_limit_exceeded","message":"secret"}}'))
        http = ChatGPTTransport()
        http.opener = Opener()
        for call in [lambda: http.json("GET", API), lambda: list(http.events({}, "secret"))]:
            with self.assertRaises(RemoteError) as error:
                call()
            self.assertEqual(error.exception.reason, "subscription_sharing_usage_limit_exceeded")
            self.assertFalse(error.exception.retryable)
            self.assertNotIn("secret", str(error.exception))


class ConfigAndCLITests(unittest.TestCase):
    def test_default_provider_and_config_relative_credential_path(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {}, clear=True):
            path = Path(root) / "orbit.toml"
            path.write_text('[model]\nprovider="chatgpt"\nname="configured"\n[chatgpt]\nauth_file="private/auth.json"\n')
            config = load_config(path)
            config.require_model()
            self.assertEqual(config.chatgpt_auth_file, str(Path(root) / "private/auth.json"))
            self.assertEqual(Config().model_provider, "openai_compatible")
            with patch.dict(os.environ, {"ORBIT_MODEL_PROVIDER": "openai_compatible"}):
                with self.assertRaises(ConfigError):
                    load_config(path).require_model()
            path.write_text('[model]\nprovider="unknown"\n')
            with self.assertRaises(ConfigError):
                load_config(path)

    def test_status_without_notion_or_database_or_credentials(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {}, clear=True):
            path = Path(root) / "orbit.toml"
            path.write_text('[runtime]\ndb_path="should-not-create.sqlite3"\n')
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(["--config", str(path), "chatgpt-status"]), 0)
            self.assertEqual(json.loads(output.getvalue())["accounts"], [])
            self.assertEqual(list(Path(root).iterdir()), [path])

    def test_missing_crypto_fails_before_authorization(self):
        with patch("orbit.chatgpt_auth.jwt_library", side_effect=AuthError("install_orbit_chatgpt_extra")):
            with tempfile.TemporaryDirectory() as root, redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
                path = Path(root) / "orbit.toml"
                self.assertEqual(main(["--config", str(path), "chatgpt-login"]), 1)
                self.assertFalse((Path(root) / "var").exists())


if __name__ == "__main__":
    unittest.main()

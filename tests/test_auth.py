"""
Phase 5: who may call Agent B (trust model §4, §9). Tokens are signed locally (tests/tokens.py) like
Keycloak's; one integration test at the end uses the real Keycloak when it is running.
"""
import asyncio
import json
import logging
import time

import jwt
import pytest
import tokens
from a2a.types import TaskState
from a2a_helpers import URL, data, http, result_of, send, status_text

from discovery_agent import auth, config

GOOD = {"contract": "discovery/1", "operation": "lookup_title", "request_id": "t-5#1", "params": {"title": ""}}
RPC = {"jsonrpc": "2.0", "id": "1", "method": "SendMessage", "params": {"message": {
    "messageId": "m1", "role": "ROLE_USER", "parts": [{"data": GOOD, "mediaType": "application/json"}]}}}


def post(token=None, raw_header=None):
    """A raw JSON-RPC POST, to see the HTTP status the middleware answers with."""
    async def go():
        async with http(token) as h:
            headers = {"Authorization": raw_header} if raw_header else {}
            return await h.post("/", json=RPC, headers=headers)
    return asyncio.run(go())


def use_policy(monkeypatch, **caller):
    base = {"operations": frozenset({"lookup_title", "lookup_titles", "search_youtube", "discover"}),
            "permissions": frozenset({"catalog:read"}), "requests_per_minute": 120, "discover_per_minute": 20}
    base.update(caller)
    monkeypatch.setattr(auth, "_policy", auth.AccessPolicy("discovery-agent",
                                                           {"concierge-agent": auth.CallerPolicy(**base)}))


# ---------------------------------------------------------------- 401: not a usable token
@pytest.mark.parametrize("token, raw, code", [
    (None, None, "NO_TOKEN"),
    (None, "Basic Z2F1cmF2OnB3", "NO_TOKEN"),
    (None, "Bearer ", "NO_TOKEN"),
    ("not-a-jwt", None, "MALFORMED"),
    (tokens.issue(key=tokens.STRANGER), None, "BAD_SIGNATURE"),           # a key the issuer never published
    (tokens.issue(algorithm="HS256"), None, "BAD_SIGNATURE"),             # symmetric-key trick
    (tokens.issue(exp=int(time.time()) - 120), None, "EXPIRED"),
    (tokens.issue(aud="catalog-mcp"), None, "WRONG_AUDIENCE"),            # a token for another server
    (tokens.issue(drop=("aud",)), None, "WRONG_AUDIENCE"),
    (tokens.issue(iss="http://evil.example/realms/household"), None, "WRONG_ISSUER"),
    (tokens.issue(drop=("sub",)), None, "MISSING_CLAIM"),
])
def test_unusable_tokens_get_401_before_any_work(token, raw, code):
    r = post(token, raw)
    assert r.status_code == 401 and r.json()["error"] == code
    assert r.headers["WWW-Authenticate"].startswith('Bearer realm="discovery-agent"')


def test_an_unsigned_alg_none_token_is_refused():
    unsigned = jwt.encode({"iss": config.OIDC_ISSUER, "aud": config.AUDIENCE, "sub": "x", "azp": "concierge-agent",
                           "scope": "discovery-agent", "iat": int(time.time()), "exp": int(time.time()) + 300},
                          None, algorithm="none")
    assert post(unsigned).status_code == 401


def test_a_401_does_not_say_why_in_detail():
    body = post(tokens.issue(aud="catalog-mcp")).json()
    assert body["message"] == "the token was not accepted" and "catalog-mcp" not in json.dumps(body)


# ---------------------------------------------------------------- 403: valid token, wrong caller
def test_a_token_without_the_scope_gets_403():
    r = post(tokens.issue(scope="openid profile"))
    assert r.status_code == 403 and r.json()["error"] == "MISSING_SCOPE"


def test_a_user_token_with_the_right_audience_is_still_refused():
    """A person's token (azp concierge-cli) that somehow carries aud discovery-agent: not an allowed caller."""
    r = post(tokens.issue(azp="concierge-cli", sub="gaurav-user-id", preferred_username="gaurav"))
    assert r.status_code == 403 and r.json()["error"] == "CALLER_NOT_ALLOWED"


def test_an_unknown_client_is_refused():
    assert post(tokens.issue(azp="some-other-app")).json()["error"] == "CALLER_NOT_ALLOWED"


# ---------------------------------------------------------------- 503: cannot check tokens
def test_an_unreachable_issuer_is_503_not_401(monkeypatch):
    def down(token):
        raise jwt.PyJWKClientConnectionError("connection refused")
    monkeypatch.setattr(auth, "_verifier", auth.TokenVerifier(config.AUDIENCE, config.OIDC_ISSUER, key_for_token=down))
    r = post(tokens.good())
    assert r.status_code == 503 and r.json()["error"] == "ISSUER_UNREACHABLE"


# ---------------------------------------------------------------- public endpoints
def test_the_card_and_health_checks_need_no_token():
    async def go():
        async with http(None) as h:
            return [await h.get(p) for p in ("/.well-known/agent-card.json", "/healthz", "/readyz")]
    card, health, ready = asyncio.run(go())
    assert card.status_code == 200 and health.status_code == 200 and ready.status_code in (200, 503)
    scheme = card.json()["securitySchemes"]["bearer"]["httpAuthSecurityScheme"]
    assert scheme["scheme"] == "Bearer" and scheme["bearerFormat"] == "JWT"
    assert card.json()["securityRequirements"][0]["schemes"] == {"bearer": {}}


# ---------------------------------------------------------------- the policy, per operation
def test_an_operation_outside_the_callers_policy_is_rejected(monkeypatch):
    use_policy(monkeypatch, operations=frozenset({"lookup_title"}))
    task = send(data({**GOOD, "operation": "search_youtube", "params": {"query": "tum hi ho"}}))
    assert task.status.state == TaskState.TASK_STATE_REJECTED
    assert status_text(task) == "FORBIDDEN_OPERATION: concierge-agent may not use search_youtube"
    assert not task.artifacts


def test_a_caller_without_the_tool_permission_never_reaches_the_catalog(monkeypatch, catalog_mode):
    catalog_mode("crash")                         # would show CATALOG_UNAVAILABLE if anything started
    use_policy(monkeypatch, permissions=frozenset())
    r = result_of(send(data({**GOOD, "params": {"title": "The Friend"}})))
    assert r["outcome"] == "provider_error" and r["error"]["code"] == "PERMISSION_DENIED"


def test_identity_in_params_or_the_envelope_changes_nothing(monkeypatch, catalog_mode):
    """The caller comes from the token. Fields that look like identity are ignored, never used."""
    catalog_mode("ok")
    use_policy(monkeypatch, operations=frozenset({"lookup_title"}))
    task = send(data({**GOOD, "caller": "admin", "azp": "root",
                      "params": {"title": "The Friend", "user": "gaurav", "permissions": ["catalog:write"]}}))
    assert result_of(task)["outcome"] == "ok"
    task = send(data({**GOOD, "operation": "search_youtube", "azp": "superuser", "params": {"query": "tum hi ho"}}))
    assert task.status.state == TaskState.TASK_STATE_REJECTED          # the token's policy still applies


# ---------------------------------------------------------------- rate limits
def test_the_per_caller_request_limit_is_429(monkeypatch):
    use_policy(monkeypatch, requests_per_minute=2)
    assert [post(tokens.good()).status_code for _ in range(3)] == [200, 200, 429]
    assert post(tokens.good()).json()["error"] == "CALLER_RATE_LIMITED"


def test_the_discover_limit_answers_rate_limited(monkeypatch):
    use_policy(monkeypatch, discover_per_minute=0)
    r = result_of(send(data({**GOOD, "operation": "discover", "params": {"question": "anything"}})))
    assert r["outcome"] == "rate_limited" and r["error"]["code"] == "CALLER_RATE_LIMITED"


def test_the_rate_window_slides():
    now = [0.0]
    lim = auth.RateLimiter(clock=lambda: now[0])
    assert [lim.allow("c", "all", 2) for _ in range(3)] == [True, True, False]
    now[0] = 61
    assert lim.allow("c", "all", 2) is True


# ---------------------------------------------------------------- the token never travels further
def test_the_call_context_holds_the_caller_but_not_the_token():
    from starlette.requests import Request
    token = tokens.good()
    caller = auth.VerifiedCaller("concierge-agent", "sa", frozenset({"discovery-agent"}), 0)
    scope = {"type": "http", "method": "POST", "path": "/", "headers": [(b"authorization", f"Bearer {token}".encode()),
             (b"x-trace", b"1")], "discovery_caller": caller}
    ctx = auth.CallerContextBuilder().build(Request(scope))
    assert ctx.state["caller"] is caller
    assert "authorization" not in {k.lower() for k in ctx.state["headers"]} and ctx.state["headers"]["x-trace"] == "1"
    assert token not in repr(ctx)


def test_audit_lines_record_decisions_and_never_tokens(caplog, catalog_mode):
    catalog_mode("ok")
    token = tokens.good()
    with caplog.at_level(logging.INFO, logger="discovery_agent"):
        post(None)
        send(data({**GOOD, "params": {"title": "The Friend"}}), token=token)
    audits = [json.loads(r.getMessage()) for r in caplog.records if r.name == "discovery_agent.audit"]
    assert {"decision": "denied", "event": "a2a.auth", "code": "NO_TOKEN", "status": 401} in audits
    assert {"decision": "allowed", "event": "a2a.auth", "client": "concierge-agent", "operation": "lookup_title",
            "request_id": "t-5#1"} in audits
    assert token not in caplog.text and token.split(".")[2] not in caplog.text


# ---------------------------------------------------------------- the access configuration
@pytest.mark.parametrize("text, problem", [
    ("{", "not valid JSON"),
    ('{"version": "1.0.0", "required_scope": "discovery-agent", "callers": {}}', "callers"),
    ('{"version": "1.0.0", "required_scope": "discovery-agent", "callers": {"concierge-agent": {"operations": '
     '["play_title"], "permissions": [], "rate_limit": {"requests_per_minute": 1, "discover_per_minute": 1}}}}',
     "play_title"),
])
def test_an_invalid_access_configuration_is_refused(tmp_path, text, problem):
    p = tmp_path / "access.json"
    p.write_text(text, encoding="utf-8")
    with pytest.raises(auth.AccessConfigError, match=problem):
        auth.load_policy(p)


def test_the_server_refuses_to_start_with_a_bad_access_config(tmp_path, monkeypatch):
    from discovery_agent import server
    (tmp_path / "access.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(auth, "ACCESS_FILE", tmp_path / "access.json")
    monkeypatch.setattr(auth, "_policy", None)
    with pytest.raises(SystemExit, match="access configuration error"):
        server.main()


# ---------------------------------------------------------------- the real Keycloak (skipped if not running)
def test_a_real_keycloak_client_credentials_token_is_accepted():
    import httpx
    token_url = f"{config.OIDC_ISSUER}/protocol/openid-connect/token"
    try:
        r = httpx.post(token_url, data={"grant_type": "client_credentials", "client_id": "concierge-agent",
                                        "client_secret": "concierge-agent-dev-secret"}, timeout=5)
    except httpx.HTTPError:
        pytest.skip(f"Keycloak not reachable at {config.OIDC_ISSUER}")
    assert r.status_code == 200, ("Keycloak is running but has no concierge-agent client: recreate the container "
                                  f"so it re-imports keycloak/household-realm.json ({r.status_code} {r.text[:200]})")
    token = r.json()["access_token"]
    caller = auth.TokenVerifier(config.AUDIENCE, config.OIDC_ISSUER).verify(token)     # the REAL JWKS
    assert caller.client == "concierge-agent" and "discovery-agent" in caller.scopes and caller.subject

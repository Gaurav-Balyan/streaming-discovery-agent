"""
Caller authentication and authorization for Agent B's A2A endpoint (Phase 5; ADR-005, trust model §4).

    every POST to the A2A endpoint, BEFORE any A2A or agent code runs (AuthMiddleware):
      1. Authorization: Bearer <JWT>                           missing           -> 401
      2. signature (RS256 via the issuer's JWKS), iss, aud=discovery-agent, exp/iat, sub
                                                               invalid / expired -> 401
                                                               issuer unreachable -> 503 (not a bad token)
      3. required scope (discovery-agent)                      missing           -> 403
      4. azp is a configured caller (config/access.json)       e.g. a user token -> 403
      5. per-caller rate limit (requests_per_minute)                             -> 429
    then, in the executor (server.py), with the operation known:
      6. operation allowed for this caller                     else task REJECTED (FORBIDDEN_OPERATION)
      7. discover_per_minute                                   else outcome rate_limited (CALLER_RATE_LIMITED)
      8. the caller's permissions feed tools.Caller            (tools.py checks them per tool call)

The verified caller travels to the executor in the A2A call context (CallerContextBuilder), never in the
request's params. The Authorization header is removed from that context so the token cannot reach logs.
Every decision is written as one audit line (logger "discovery_agent.audit"); tokens are never logged.

Token checks are copied from Agent A's common/auth.py TokenVerifier (ADR-002: no shared package).
"""
import json
import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass

import anyio
import jwt  # PyJWT
from a2a.server.context import ServerCallContext
from a2a.server.routes.common import DefaultServerCallContextBuilder
from jsonschema import Draft202012Validator
from starlette.responses import JSONResponse

from discovery_agent import config
from discovery_agent.contract import first_problem

log = logging.getLogger("discovery_agent.auth")
audit_log = logging.getLogger("discovery_agent.audit")

ALGORITHMS = ["RS256"]          # never "none", never HS256
LEEWAY_SECONDS = 30
ACCESS_FILE = config.BASE_DIR / "config" / "access.json"
ACCESS_SCHEMA = json.loads((config.BASE_DIR / "schemas" / "access.schema.json").read_text(encoding="utf-8"))
Draft202012Validator.check_schema(ACCESS_SCHEMA)


class AuthError(Exception):
    MALFORMED = "MALFORMED"
    UNKNOWN_KEY = "UNKNOWN_KEY"
    BAD_SIGNATURE = "BAD_SIGNATURE"
    WRONG_ISSUER = "WRONG_ISSUER"
    WRONG_AUDIENCE = "WRONG_AUDIENCE"
    EXPIRED = "EXPIRED"
    MISSING_CLAIM = "MISSING_CLAIM"

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


class AccessConfigError(Exception):
    pass


@dataclass(frozen=True)
class VerifiedCaller:
    client: str             # azp: which application asked (e.g. concierge-agent)
    subject: str            # sub: the service account's id
    scopes: frozenset
    expires_at: int


@dataclass(frozen=True)
class CallerPolicy:
    operations: frozenset
    permissions: frozenset
    requests_per_minute: int
    discover_per_minute: int


@dataclass(frozen=True)
class AccessPolicy:
    required_scope: str
    callers: dict


class TokenVerifier:
    """Checks a JWT for ONE audience. `key_for_token`: token -> public key (default: the issuer's JWKS)."""

    def __init__(self, audience: str, issuer: str, key_for_token=None):
        self.audience, self.issuer = audience, issuer
        if key_for_token is None:
            jwks = jwt.PyJWKClient(f"{issuer}/protocol/openid-connect/certs", cache_keys=True)
            key_for_token = lambda token: jwks.get_signing_key_from_jwt(token).key  # noqa: E731
        self._key_for_token = key_for_token

    def verify(self, token: str) -> VerifiedCaller:
        try:
            key = self._key_for_token(token)
        except jwt.PyJWKClientConnectionError:
            raise                                   # issuer unreachable: infrastructure, not a bad token
        except jwt.PyJWKClientError as e:
            raise AuthError(AuthError.UNKNOWN_KEY, str(e)) from None
        except jwt.DecodeError as e:
            raise AuthError(AuthError.MALFORMED, str(e)) from None
        try:
            claims = jwt.decode(token, key, algorithms=ALGORITHMS, audience=self.audience, issuer=self.issuer,
                                leeway=LEEWAY_SECONDS, options={"require": ["exp", "iat", "iss", "aud", "sub"]})
        except jwt.ExpiredSignatureError as e:
            raise AuthError(AuthError.EXPIRED, str(e)) from None
        except jwt.InvalidAudienceError:
            raise AuthError(AuthError.WRONG_AUDIENCE, f"token is not for {self.audience}") from None
        except jwt.InvalidIssuerError as e:
            raise AuthError(AuthError.WRONG_ISSUER, str(e)) from None
        except jwt.MissingRequiredClaimError as e:
            code = AuthError.WRONG_AUDIENCE if e.claim == "aud" else AuthError.MISSING_CLAIM
            raise AuthError(code, str(e)) from None
        except (jwt.InvalidSignatureError, jwt.InvalidAlgorithmError) as e:
            raise AuthError(AuthError.BAD_SIGNATURE, str(e)) from None
        except jwt.DecodeError as e:
            raise AuthError(AuthError.MALFORMED, str(e)) from None
        return VerifiedCaller(client=str(claims.get("azp", "")), subject=str(claims["sub"]),
                              scopes=frozenset(str(claims.get("scope", "")).split()), expires_at=int(claims["exp"]))


def load_policy(path=None) -> AccessPolicy:
    path = path or ACCESS_FILE
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise AccessConfigError(f"{path.name}: not found") from None
    except json.JSONDecodeError as e:
        raise AccessConfigError(f"{path.name}: not valid JSON ({e.msg})") from None
    problem = first_problem(Draft202012Validator(ACCESS_SCHEMA), raw)
    if problem:
        raise AccessConfigError(f"{path.name}: {problem}")
    callers = {name: CallerPolicy(frozenset(c["operations"]), frozenset(c["permissions"]),
                                  c["rate_limit"]["requests_per_minute"], c["rate_limit"]["discover_per_minute"])
               for name, c in raw["callers"].items()}
    return AccessPolicy(raw["required_scope"], callers)


class RateLimiter:
    """Sliding one-minute window per (caller, bucket). In memory: one Agent B process (Phase 8 revisits)."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self._hits: dict[tuple, deque] = defaultdict(deque)

    def allow(self, caller: str, bucket: str, per_minute: int) -> bool:
        now, window = self.clock(), self._hits[(caller, bucket)]
        while window and now - window[0] >= 60:
            window.popleft()
        if len(window) >= per_minute:
            return False
        window.append(now)
        return True


def audit(decision: str, **fields) -> None:
    """One JSON line per decision. Never tokens; ids and codes only."""
    audit_log.info(json.dumps({"event": "a2a.auth", "decision": decision,
                               **{k: v for k, v in fields.items() if v is not None}}, sort_keys=True))


# ---------------------------------------------------------------- shared state (replaced in tests)
_verifier: TokenVerifier | None = None
_policy: AccessPolicy | None = None
limiter = RateLimiter()


def verifier() -> TokenVerifier:
    global _verifier
    if _verifier is None:
        _verifier = TokenVerifier(config.AUDIENCE, config.OIDC_ISSUER)
    return _verifier


def policy() -> AccessPolicy:
    global _policy
    if _policy is None:
        _policy = load_policy()
    return _policy


# ---------------------------------------------------------------- the middleware
def _deny(status: int, code: str, message: str) -> JSONResponse:
    headers = {}
    if status == 401:
        headers["WWW-Authenticate"] = (f'Bearer realm="{config.AUDIENCE}", error="invalid_token", '
                                       f'error_description="{code}"')
    return JSONResponse({"error": code, "message": message}, status_code=status, headers=headers)


class AuthMiddleware:
    """Pure ASGI middleware. Protects POSTs to the A2A endpoint; the Agent Card and health checks stay public."""

    def __init__(self, app, protected_paths=("/",)):
        self.app, self.protected = app, set(protected_paths)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST" or scope["path"] not in self.protected:
            return await self.app(scope, receive, send)
        response, caller = await self._check(scope)
        if response is not None:
            return await response(scope, receive, send)
        scope["discovery_caller"] = caller
        return await self.app(scope, receive, send)

    async def _check(self, scope):
        header = dict(scope.get("headers") or []).get(b"authorization", b"").decode("latin-1")
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            audit("denied", status=401, code="NO_TOKEN")
            return _deny(401, "NO_TOKEN", "a bearer token for discovery-agent is required"), None
        try:
            caller = await anyio.to_thread.run_sync(verifier().verify, token.strip())
        except AuthError as e:
            audit("denied", status=401, code=e.code)
            return _deny(401, e.code, "the token was not accepted"), None
        except jwt.PyJWKClientConnectionError:
            audit("denied", status=503, code="ISSUER_UNREACHABLE")
            log.error("cannot reach the token issuer's keys (%s)", config.OIDC_ISSUER)
            return _deny(503, "ISSUER_UNREACHABLE", "tokens cannot be checked right now"), None
        pol = policy()
        if pol.required_scope not in caller.scopes:
            audit("denied", status=403, code="MISSING_SCOPE", client=caller.client)
            return _deny(403, "MISSING_SCOPE", f"the token lacks scope {pol.required_scope}"), None
        cp = pol.callers.get(caller.client)
        if cp is None:
            audit("denied", status=403, code="CALLER_NOT_ALLOWED", client=caller.client)
            return _deny(403, "CALLER_NOT_ALLOWED", "this client may not call Agent B"), None
        if not limiter.allow(caller.client, "all", cp.requests_per_minute):
            audit("denied", status=429, code="CALLER_RATE_LIMITED", client=caller.client)
            return _deny(429, "CALLER_RATE_LIMITED", "too many requests; slow down"), None
        return None, caller


class CallerContextBuilder(DefaultServerCallContextBuilder):
    """Puts the verified caller into the A2A call context and strips the token from the headers."""

    def build(self, request) -> ServerCallContext:
        ctx = super().build(request)
        headers = {k: v for k, v in (ctx.state.get("headers") or {}).items() if k.lower() != "authorization"}
        ctx.state["headers"] = headers
        ctx.state["caller"] = request.scope.get("discovery_caller")
        return ctx

"""
Test tokens, signed like Keycloak's (RS256) with a key made for the test run. No Keycloak needed.
Agent B's verifier is pointed at TRUSTED's public key by the autouse fixture in conftest.py.
"""
import time
import uuid

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from discovery_agent import config

TRUSTED = rsa.generate_private_key(public_exponent=65537, key_size=2048)
STRANGER = rsa.generate_private_key(public_exponent=65537, key_size=2048)     # a key the issuer never published


def issue(key=None, algorithm="RS256", drop=(), **overrides) -> str:
    now = int(time.time())
    claims = {"iss": config.OIDC_ISSUER, "aud": config.AUDIENCE, "sub": "service-account-concierge-agent",
              "azp": "concierge-agent", "scope": "discovery-agent", "iat": now, "exp": now + 300,
              "jti": uuid.uuid4().hex}
    claims.update(overrides)
    for name in drop:
        claims.pop(name, None)
    if algorithm == "HS256":
        return jwt.encode(claims, "a-shared-secret-anyone-could-guess", algorithm="HS256")
    return jwt.encode(claims, key or TRUSTED, algorithm=algorithm)


def good() -> str:
    return issue()

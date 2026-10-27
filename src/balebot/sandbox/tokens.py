"""Short-lived bearer tokens a sandbox uses to call the LLM proxy.

A token only says "user N may make up to M model calls until time T". It never contains or
unlocks any provider key: the proxy looks the key up server-side.
"""

import base64
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass


class BadToken(Exception):
    pass


@dataclass(frozen=True)
class AgentToken:
    user_id: int
    run_id: str
    expires: int
    max_calls: int


def _sign(secret: str, payload: str) -> str:
    mac = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac[:18]).decode()


def issue(secret: str, user_id: int, *, ttl: int, max_calls: int) -> str:
    if len(secret) < 16:
        raise ValueError("proxy secret too short")
    payload = f"{user_id}.{secrets.token_hex(6)}.{int(time.time()) + ttl}.{max_calls}"
    return f"bbt.{payload}.{_sign(secret, payload)}"


def verify(secret: str, token: str, *, now: float | None = None) -> AgentToken:
    parts = token.split(".")
    if len(parts) != 6 or parts[0] != "bbt" or len(secret) < 16:
        raise BadToken("malformed")
    user, run, exp, calls, sig = parts[1:]
    payload = ".".join(parts[1:5])
    if not hmac.compare_digest(sig, _sign(secret, payload)):
        raise BadToken("signature")
    if not (user.isdigit() and exp.isdigit() and calls.isdigit()):
        raise BadToken("malformed")
    if int(exp) < (now if now is not None else time.time()):
        raise BadToken("expired")
    return AgentToken(int(user), run, int(exp), int(calls))


def issue_user_token(secret: str, user_id: int, scope: str = "sandbox") -> str:
    if len(secret) < 16:
        raise ValueError("proxy secret too short")
    payload = f"{user_id}.{scope}"
    return f"bbu.{payload}.{_sign(secret, payload)}"


def verify_user_token(secret: str, raw: str, scope: str = "sandbox") -> int:
    parts = raw.split(".")
    if len(parts) != 4 or parts[0] != "bbu" or len(secret) < 16:
        raise BadToken("malformed")
    user, tok_scope, sig = parts[1:]
    payload = f"{user}.{tok_scope}"
    if not hmac.compare_digest(sig, _sign(secret, payload)):
        raise BadToken("signature")
    if tok_scope != scope or not user.isdigit():
        raise BadToken("malformed")
    return int(user)


def verify_any_token(secret: str, raw: str) -> int:
    if raw.startswith("bbt."):
        tok = verify(secret, raw)
        return tok.user_id
    if raw.startswith("bbu."):
        return verify_user_token(secret, raw)
    raise BadToken("unknown token prefix")

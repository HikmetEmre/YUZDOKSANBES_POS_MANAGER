# auth.py
# =====================================================
# Server-enforced roles (see ARCHITECTURE.md §3).
#
# Three roles:
#   - "customer" : QR surface. No token. Order-only, orders land 'pending'.
#   - "waiter"   : staff phone. Can create/accept orders, set prep status.
#   - "checkout" : desk. Full power — payment, cancel, revenue, management.
#
# How it works:
#   - Staff POST their PIN to /auth. The server matches it to a role and
#     returns a SIGNED token (role + expiry, HMAC-signed with SECRET_KEY).
#   - The client sends that token on every request (Authorization header).
#   - Protected endpoints declare which roles may call them; the server
#     verifies the token's signature + role. The client cannot forge a
#     higher role because it can't produce a valid signature without the key.
#
# No external dependencies — uses stdlib hmac/hashlib/base64.
# =====================================================

from datetime import datetime, timedelta, timezone
from typing import Optional
import hashlib
import hmac
import base64
import json

from fastapi import Header, HTTPException

import config

# Token lifetime. Staff re-enter their PIN after this (per device).
TOKEN_TTL_HOURS = 24

# Role power ranking (higher = more power). 'checkout' can do anything a
# 'waiter' can, so endpoints that allow waiter also allow checkout.
ROLE_RANK = {"customer": 0, "waiter": 1, "checkout": 2}


def _sign(payload_b64: str) -> str:
    sig = hmac.new(config.SECRET_KEY.encode(), payload_b64.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(sig).decode().rstrip("=")


def make_token(role: str) -> str:
    """Create a signed token carrying the role and an expiry timestamp."""
    exp = (datetime.now(timezone.utc) + timedelta(hours=TOKEN_TTL_HOURS)).timestamp()
    payload = {"role": role, "exp": exp}
    payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"{payload_b64}.{_sign(payload_b64)}"


def role_for_pin(pin: str) -> Optional[str]:
    """Map a submitted PIN to a role, or None if it matches nothing.
    Uses constant-time comparison so response timing can't reveal how many
    leading digits of a PIN were guessed correctly."""
    pin = pin or ""
    # Compare against BOTH PINs every time (no early return) to keep timing flat.
    is_checkout = hmac.compare_digest(pin, config.CHECKOUT_PIN)
    is_waiter   = hmac.compare_digest(pin, config.WAITER_PIN)
    if is_checkout:
        return "checkout"
    if is_waiter:
        return "waiter"
    return None


def _b64_pad(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def verify_token(token: Optional[str]) -> str:
    """
    Return the role encoded in a valid token. Falls back to 'customer'
    (no powers) when there is no token — that's the QR surface.
    Raises 401 only if a token is present but invalid/expired.
    """
    if not token:
        return "customer"
    try:
        payload_b64, sig = token.split(".", 1)
    except ValueError:
        raise HTTPException(status_code=401, detail="Geçersiz oturum")

    # Constant-time signature check.
    if not hmac.compare_digest(sig, _sign(payload_b64)):
        raise HTTPException(status_code=401, detail="Geçersiz oturum")

    try:
        payload = json.loads(_b64_pad(payload_b64))
    except Exception:
        raise HTTPException(status_code=401, detail="Geçersiz oturum")

    if datetime.now(timezone.utc).timestamp() > payload.get("exp", 0):
        raise HTTPException(status_code=401, detail="Oturum süresi doldu, tekrar giriş yapın")

    role = payload.get("role", "customer")
    if role not in ROLE_RANK:
        return "customer"
    return role


def current_role(authorization: Optional[str] = Header(default=None)) -> str:
    """
    FastAPI dependency: extract & verify the role from the Authorization
    header ('Bearer <token>'). No header → 'customer'.
    """
    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:]
    return verify_token(token)


def require(min_role: str):
    """
    Dependency factory: require AT LEAST the given role.
    Usage:  @app.post(..., dependencies=[Depends(require("checkout"))])
    """
    needed = ROLE_RANK[min_role]

    # A closure that itself depends on current_role (which verifies the token).
    from fastapi import Depends

    def checker(role: str = Depends(current_role)) -> str:
        if ROLE_RANK.get(role, 0) < needed:
            raise HTTPException(status_code=403, detail="Bu işlem için yetkiniz yok")
        return role

    return checker

"""
Reptile access gate — handshake authentication for network-exposed deployments.

Goal (user design, confirmed feasible): when Reptile is served on a network
(host 0.0.0.0), a client must complete a handshake with the server-configured
access password BEFORE any page content or API is reachable. Three wrong
attempts lock the client out — without a valid access token ("密钥") the page
content cannot be loaded.

Design — challenge/response so the password itself never crosses the wire:

  1. Client asks for a challenge  → server returns a one-time random `nonce`.
  2. Client computes a proof      → HMAC-SHA256(key = SHA256(password), msg = nonce)
                                     and sends back {nonce, proof}.
  3. Server recomputes the proof from its own password and compares in constant
     time. On success it issues a signed, expiring access token (the "key");
     on failure it counts the attempt. After MAX_ATTEMPTS failures the client
     (identified by IP) is locked out for LOCK_SECONDS.

The token is an HMAC-signed `payload.signature` string verified on every
request via cookie or header. Tokens are signed with a per-process secret
derived from the password, so restarting the server or changing the password
invalidates outstanding tokens.

Stdlib only (hmac / hashlib / secrets) — no third-party crypto dependency.

Enablement: set the environment variable ``REPTILE_ACCESS_PASSWORD``. When it
is unset/empty the gate is DISABLED and Reptile behaves exactly as before
(safe for trusted local use); when it is set the gate is enforced.
"""

import os
import time
import json
import hmac
import base64
import hashlib
import secrets
import threading

# ── Configuration ─────────────────────────────────────────────────────────────
ACCESS_PASSWORD = os.environ.get("REPTILE_ACCESS_PASSWORD", "").strip()

MAX_ATTEMPTS   = 3          # wrong tries before lockout (user spec: 3)
LOCK_SECONDS   = 300        # lockout window after MAX_ATTEMPTS failures (5 min)
CHALLENGE_TTL  = 120        # a nonce is valid for 2 minutes
TOKEN_TTL      = 12 * 3600  # an issued access token lives 12 hours

COOKIE_NAME    = "reptile_access"
HEADER_NAME    = "x-reptile-access"

# Per-process random salt: stable for the lifetime of the server, so tokens
# stay valid across requests but are invalidated on restart.
_RUNTIME_SALT = secrets.token_bytes(16)

_lock        = threading.Lock()
_challenges  = {}   # nonce -> expiry timestamp
_attempts    = {}   # client_id -> {"count": int, "locked_until": float}


# ── Helpers ───────────────────────────────────────────────────────────────────

def gate_enabled() -> bool:
    """True when an access password is configured (gate enforced)."""
    return bool(ACCESS_PASSWORD)


def _now() -> float:
    return time.time()


def _password_key() -> bytes:
    """The HMAC key shared (in derived form) with the client: SHA256(password)."""
    return hashlib.sha256(ACCESS_PASSWORD.encode("utf-8")).digest()


def _server_secret() -> bytes:
    """Secret used to sign access tokens — bound to both the runtime salt and
    the password so tokens die on restart or password change."""
    return hashlib.sha256(_RUNTIME_SALT + _password_key()).digest()


def expected_proof(nonce: str) -> str:
    """The proof a correct client must produce for `nonce`."""
    return hmac.new(_password_key(), nonce.encode("utf-8"),
                    hashlib.sha256).hexdigest()


# ── Challenges ────────────────────────────────────────────────────────────────

def _purge_challenges():
    now = _now()
    for n in [n for n, exp in _challenges.items() if exp < now]:
        _challenges.pop(n, None)


def new_challenge() -> dict:
    """Mint a one-time nonce for a fresh handshake attempt."""
    with _lock:
        _purge_challenges()
        nonce = secrets.token_hex(24)
        _challenges[nonce] = _now() + CHALLENGE_TTL
        return {"nonce": nonce, "ttl": CHALLENGE_TTL}


# ── Lockout tracking ──────────────────────────────────────────────────────────

def _state(client_id: str) -> dict:
    return _attempts.setdefault(client_id, {"count": 0, "locked_until": 0.0})


def lock_remaining(client_id: str) -> int:
    """Seconds remaining on a client's lockout (0 if not locked)."""
    st = _attempts.get(client_id)
    if not st:
        return 0
    rem = st["locked_until"] - _now()
    return int(rem) + 1 if rem > 0 else 0


def attempts_left(client_id: str) -> int:
    st = _attempts.get(client_id)
    used = st["count"] if st else 0
    return max(0, MAX_ATTEMPTS - used)


# ── Verification ──────────────────────────────────────────────────────────────

def verify(client_id: str, nonce: str, proof: str) -> dict:
    """Validate a handshake proof. Returns a result dict describing the outcome:
      success → {"ok": True, "token": "<access token>"}
      wrong   → {"ok": False, "attempts_left": n}
      locked  → {"ok": False, "locked": True, "retry_after": secs}
      stale   → {"ok": False, "stale": True, "attempts_left": n}  (expired nonce)
    """
    with _lock:
        rem = lock_remaining(client_id)
        if rem > 0:
            return {"ok": False, "locked": True, "retry_after": rem,
                    "attempts_left": 0}

        exp = _challenges.get(nonce)
        if not exp or exp < _now():
            _challenges.pop(nonce, None)
            # An expired/unknown nonce is not a wrong password — don't penalise.
            return {"ok": False, "stale": True,
                    "attempts_left": attempts_left(client_id)}
        # One-time use: consume the nonce regardless of outcome.
        _challenges.pop(nonce, None)

        st = _state(client_id)
        if hmac.compare_digest(proof or "", expected_proof(nonce)):
            st["count"] = 0
            st["locked_until"] = 0.0
            return {"ok": True, "token": issue_token()}

        st["count"] += 1
        if st["count"] >= MAX_ATTEMPTS:
            st["locked_until"] = _now() + LOCK_SECONDS
            return {"ok": False, "locked": True, "retry_after": LOCK_SECONDS,
                    "attempts_left": 0}
        return {"ok": False, "attempts_left": MAX_ATTEMPTS - st["count"]}


# ── Tokens ────────────────────────────────────────────────────────────────────

def issue_token() -> str:
    payload = {"exp": int(_now() + TOKEN_TTL)}
    raw = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).rstrip(b"=")
    sig = hmac.new(_server_secret(), raw, hashlib.sha256).hexdigest()
    return raw.decode("ascii") + "." + sig


def valid_token(token: str) -> bool:
    if not token or "." not in token:
        return False
    raw, _, sig = token.partition(".")
    try:
        expected = hmac.new(_server_secret(), raw.encode("ascii"),
                            hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return False
        pad = "=" * (-len(raw) % 4)
        payload = json.loads(base64.urlsafe_b64decode(raw + pad))
        return float(payload.get("exp", 0)) > _now()
    except Exception:
        return False


# ── Request helpers ───────────────────────────────────────────────────────────

def client_id_from_request(request) -> str:
    """Identify the client for lockout accounting. Prefers the first hop in
    X-Forwarded-For (when behind a proxy), else the socket peer."""
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def token_from_request(request) -> str:
    """Extract the access token from the cookie or the header."""
    return (request.cookies.get(COOKIE_NAME)
            or request.headers.get(HEADER_NAME, "")
            or "")


def request_authorized(request) -> bool:
    """True when the gate is disabled, or the request carries a valid token."""
    if not gate_enabled():
        return True
    return valid_token(token_from_request(request))

import hashlib
import hmac
import os
import time
import secrets
import string
import jwt
from fastapi import HTTPException, Header
from . import config


def hash_password(password: str, salt: str = None) -> str:
    salt = salt or os.urandom(16).hex()
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000)
    return f"{salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    if not stored:
        return False
    try:
        salt, digest_hex = stored.split("$", 1)
    except ValueError:
        return False
    check = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000)
    return hmac.compare_digest(check.hex(), digest_hex)


def random_username(prefix: str = "user") -> str:
    suffix = "".join(secrets.choice(string.digits) for _ in range(6))
    return f"{prefix}{suffix}"


def random_password(length: int = 10) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


# ---------- admin tokens ----------
def create_admin_token(username: str) -> str:
    payload = {
        "sub": username,
        "type": "admin",
        "iat": int(time.time()),
        "exp": int(time.time()) + 60 * 60 * 24 * 7,
    }
    return jwt.encode(payload, config.JWT_SECRET, algorithm="HS256")


# ---------- peer (self-service portal) tokens ----------
def create_peer_token(peer_id: int) -> str:
    payload = {
        "sub": str(peer_id),
        "type": "peer",
        "iat": int(time.time()),
        "exp": int(time.time()) + 60 * 60 * 24,  # 1 day
    }
    return jwt.encode(payload, config.JWT_SECRET, algorithm="HS256")


def _decode(token: str) -> dict:
    try:
        return jwt.decode(token, config.JWT_SECRET, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")


def require_admin(authorization: str = Header(None)) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Login required")
    payload = _decode(authorization.split(" ", 1)[1])
    if payload.get("type") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return payload["sub"]


def require_peer(authorization: str = Header(None)) -> int:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Login required")
    payload = _decode(authorization.split(" ", 1)[1])
    if payload.get("type") != "peer":
        raise HTTPException(status_code=403, detail="Invalid token type")
    return int(payload["sub"])

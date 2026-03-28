"""Authentication helpers — JWT tokens + bcrypt password hashing."""

import os
import time
import bcrypt
import jwt

# Secret for signing JWTs — generate a stable one on first run
_ENV_PATH = os.path.join(os.path.dirname(__file__), ".env")

def _load_jwt_secret() -> str:
    """Load JWT_SECRET from .env, or generate and persist one."""
    # Check env var first
    secret = os.environ.get("JWT_SECRET")
    if secret:
        return secret

    # Try to read from .env
    if os.path.exists(_ENV_PATH):
        with open(_ENV_PATH, "r") as f:
            for line in f:
                line = line.strip()
                if line.startswith("JWT_SECRET="):
                    secret = line.split("=", 1)[1].strip()
                    if secret:
                        return secret

    # Generate and append to .env
    import secrets
    secret = secrets.token_hex(32)
    with open(_ENV_PATH, "a") as f:
        f.write(f"\n# JWT signing secret (auto-generated)\nJWT_SECRET={secret}\n")
    return secret


JWT_SECRET = _load_jwt_secret()
JWT_ALGORITHM = "HS256"
TOKEN_EXPIRY_SECONDS = 60 * 60 * 24 * 30  # 30 days


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())


def create_token(user_id: int, username: str) -> str:
    payload = {
        "sub": str(user_id),
        "username": username,
        "exp": int(time.time()) + TOKEN_EXPIRY_SECONDS,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> dict | None:
    """Returns payload dict or None if invalid/expired."""
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        payload["sub"] = int(payload["sub"])  # restore int user_id
        return payload
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        return None

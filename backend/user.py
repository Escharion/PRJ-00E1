"""Authentication helpers for the private PRJ-00E1 theater."""

import hashlib
import hmac


# Keep the shared password out of plaintext; the salt is fixed so this digest
# can be verified across server restarts while the seat key changes each run.
PASSWORD_SALT = b"PRJ-00E1-seat-password-v1"
PASSWORD_HASH = bytes.fromhex(
    "5b1d733c47239c7e053d868cce9fabce"
    "ae302a1b021a96783637eb1b4e9241e2"
)
PASSWORD_ITERATIONS = 310_000
MAX_USERNAME_LENGTH = 24


def user_login(username: str, seat_key: str, password: str, current_key: str) -> bool:
    """Return whether the supplied credentials match this theater run."""
    normalized_username = " ".join(username.split())
    if not normalized_username or len(normalized_username) > MAX_USERNAME_LENGTH:
        return False
    if not hmac.compare_digest(seat_key.strip(), current_key):
        return False

    candidate_hash = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), PASSWORD_SALT, PASSWORD_ITERATIONS
    )
    return hmac.compare_digest(candidate_hash, PASSWORD_HASH)
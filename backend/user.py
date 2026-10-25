"""Authentication helpers for the private PRJ-00E1 theater."""

import hashlib
import hmac


# Keep the shared password out of plaintext; the salt and digest are fixed
# so the configured guest password can be verified across server restarts.
DEFAULT_SEAT_KEY = "EGESHOME"
PASSWORD_SALT = b"PRJ-00E1-seat-password-v1"
PASSWORD_HASH = bytes.fromhex(
    "19fd57e63deaacf2e1a43749b9c1a478"
    "737e336b9d7a757fc1dadf14c7b97ca5"
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
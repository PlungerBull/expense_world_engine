"""Shared PAT constants and hashing.

Both ``app/deps.py`` (authentication) and ``app/helpers/pat.py``
(creation) need to hash PATs the same way. Keeping the prefix and
hashing in one module prevents drift between the two call sites — if
the prefix or algorithm ever changes, there is exactly one place to
update it, and the change applies atomically to both sides.
"""

import hashlib

# Engine-scoped prefix. Enables:
#   * O(1) discrimination between PATs and Supabase JWTs in get_current_user
#     (no try/catch fallback chain).
#   * Leak-scanning by GitHub/GitGuardian/etc. — secret-scanner vendors
#     register known prefixes to alert on accidental public commits.
PAT_PREFIX = "ewe_pat_"

# Length of the display prefix stored in token_prefix. Captures the
# type marker plus the first 4 random chars — enough to disambiguate
# tokens in a list UI without leaking enough entropy to be useful.
PAT_PREFIX_LEN = len(PAT_PREFIX) + 4


def hash_pat(plaintext: str) -> str:
    """SHA-256 hex digest of a PAT plaintext.

    Plain SHA-256 (no salt, no bcrypt/argon2) is sufficient because
    PATs carry 256 bits of entropy — brute-forcing the preimage from
    a leaked hash is infeasible. The password-hashing constructions
    exist to protect low-entropy secrets, which PATs are not.
    """
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()

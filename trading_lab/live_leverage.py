"""R2.1.15 (D168 p. 3, D170, D171): the highest leverage a frozen version may take at a Start, by its recipe_sha256.

Outside every core hash, like live_denylist.py: a change here changes no hash. Every season that trades above 1x
writes this table's sha256, and each bot's leverage and limit, into its match_manifest (plan check p. 4).

A constant, not a file read at import: the bundle for the other PC ships trading_lab/*.py only."""
import hashlib
from pathlib import Path

MAX_LEVERAGE = 10

LEVERAGE_LIMITS = {
    # recipe_sha256: (limit, version), as in L1 (D170) and the arena catalogue of the other PC
    "87facc50e7f925638106c9572bcaf1abb17295fd428418b59cede3b4c09f0191": (10, "Sol V1"),
    "0e02ec05cda82b1beb0dfc6a048d14586d2996cafb2d3a064e2b01d08ba55a4e": (10, "Astra V1"),
    "0eb565d61eff018731805c9abd06477d47a2f33e27be639c934192abe6dee95e": (10, "Opus V1"),
    "d744a6f52356d446e740090d35eb8dc520b7cdcb4c238722265140125edd7752": (8, "H10"),
    "5531356533a1e786545ae4be0b8d9b6a423e86e36ab973e53e00bf930342a45f": (3, "Гібрид ETH"),
    "3328c4e29c143ab385ff670d972c777b9ddaae240410d507d5500cf750109913": (3, "Гібрид SOL"),  # D171: not 4x
}


def limit(recipe_sha256):
    """The version's limit; 1 for a recipe the table does not list. Never above MAX_LEVERAGE."""
    row = LEVERAGE_LIMITS.get(recipe_sha256)
    return min(MAX_LEVERAGE, int(row[0])) if row else 1


def limits():
    """{recipe_sha256: limit} of every listed version, for the Start form."""
    return {sha: limit(sha) for sha in LEVERAGE_LIMITS}


def _sha256():
    return hashlib.sha256(Path(__file__).read_bytes().replace(b"\r\n", b"\n")).hexdigest()


TABLE_SHA256 = _sha256()  # RC2 (LL-06): the bytes this process loaded, not the file at a later call


def table_sha256():
    """This module's bytes as imported, CRLF as LF: the same on both PCs whatever the checkout does with line ends."""
    return TABLE_SHA256

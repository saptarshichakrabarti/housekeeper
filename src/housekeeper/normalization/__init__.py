"""Deterministic, versioned format-aware normalization profiles.

Each profile documents exactly what information it discards (``loss_characteristics``) so a
normalized-hash match is never mistaken for byte identity. Normalization is used to detect
Tier-2 (normalized-exact) and Tier-3 (strong-equivalence) relationships; the raw cryptographic
hash always remains authoritative.
"""

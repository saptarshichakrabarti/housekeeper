"""Scalable near-duplicate document detection via shingling + MinHash + LSH.

Dependency-free and deterministic. MinHash/LSH generate *candidates* only; a final
relationship additionally requires exact shingle-Jaccard verification, so a fuzzy signature is
never treated as proof.
"""

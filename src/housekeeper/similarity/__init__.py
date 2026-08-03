"""Scalable near-duplicate document detection via shingling + MinHash + LSH.

Dependency-free and deterministic. MinHash/LSH generate candidates only; relationships also
require exact shingle-Jaccard verification.
"""

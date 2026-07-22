"""Content-defined chunking for partial-content overlap (Tier-4 evidence).

Chunking is opt-in and scoped: only eligible large files are chunked, candidate pairs come
from an inverted chunk index (never all-pairs), and derived indexes can be cleared and
regenerated without ever touching source files.
"""

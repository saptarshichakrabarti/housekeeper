"""Collection-level reasoning: backup marginal value, record series, and clustering.

Collections are reasoned about as wholes: a backup's value is its *unique* contribution (bytes,
content objects, protected/unresolved items), not its raw size. Removal is only ever simulated
non-destructively; nothing here moves or deletes files.
"""

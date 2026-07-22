# Canonical Roles

Housekeeper replaces the idea of a single "canonical copy" with **role-based** preservation
assignments stored in `canonical_assignments`. A group may fill several roles with different
files, and review movement must never remove all copies fulfilling a required role without
explicit acknowledgement.

## Roles

`PRESERVATION_MASTER`, `EDITABLE_SOURCE`, `FINAL_EXPORT`, `PUBLISHED_COPY`, `ACCESS_COPY`,
`BEST_METADATA_COPY`, `HIGHEST_FIDELITY_COPY`, `CANONICAL_LOCATION`.

## Implemented this pass

- **`CANONICAL_LOCATION`** — every exact-duplicate group's existing canonical entry is given
  this role. The v4 → v5 migration backfills it for pre-existing groups; `canonical assign`
  (or running an equivalence analysis) keeps it in sync for new groups. This preserves existing
  canonical behavior while opening room for richer roles.
- **`HIGHEST_FIDELITY_COPY` / `BEST_METADATA_COPY`** — for each `PIXEL_IDENTICAL` image group,
  the highest-resolution copy and the richest-EXIF copy are protected. This ensures a
  re-encoded, metadata-stripped variant never displaces the master.

Deferred: `EDITABLE_SOURCE` / `FINAL_EXPORT` scoring from derivation families, full component
score model, and dashboard role editing.

## Survival constraint

`canonical.roles.roles_lost_if_moved(database, approved_entry_ids)` returns every role that
would lose **all** its assigned copies if the approved entries were moved into review. This is
the hook for manifest validation: an approved movement that would erase a required role must be
flagged for explicit acknowledgement. (The existing exact-duplicate mover already independently
refuses to move the last verified copy of a content object.)

## User overrides

`review canonical SESSION_ID GROUP_ID ENTRY_ID` records a canonical override in the immutable
review history (existing behavior). Role changes are additive and auditable via
`canonical_assignments` (`source`, `created_at`, `superseded_at`).

## CLI

```
housekeeper canonical assign          # (re)compute location + image-metadata roles
housekeeper canonical list            # recent assignments
housekeeper canonical show GROUP_ID [--type EXACT_DUPLICATE_GROUP]
housekeeper canonical explain GROUP_ID [--type PIXEL_IDENTICAL_GROUP]
```

## What this does not prove

Role assignment is advisory preservation guidance, not a legal retention determination, and
never permanently deletes or auto-moves anything.

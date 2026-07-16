from dataclasses import dataclass


@dataclass(frozen=True)
class ReviewFilter:
    classification: str | None = None
    source_root_id: int | None = None
    minimum_confidence: float | None = None
    maximum_confidence: float | None = None
    suffix: str | None = None
    minimum_size: int | None = None
    maximum_size: int | None = None
    decision: str | None = None
    reason_code: str | None = None
    minimum_age_timestamp: float | None = None
    maximum_age_timestamp: float | None = None
    duplicate_only: bool = False
    project_only: bool = False
    stale: bool | None = None
    protected: bool | None = None
    top_level_directory: str | None = None

    def where_clause(self) -> tuple[str, tuple[object, ...]]:
        clauses = ["e.entry_type='file'"]
        params: list[object] = []
        if self.classification:
            clauses.append("c.classification=?")
            params.append(self.classification)
        if self.minimum_confidence is not None:
            clauses.append("c.confidence>=?")
            params.append(self.minimum_confidence)
        if self.maximum_confidence is not None:
            clauses.append("c.confidence<=?")
            params.append(self.maximum_confidence)
        if self.suffix:
            clauses.append("e.suffix=?")
            params.append(self.suffix.lower())
        if self.minimum_size is not None:
            clauses.append("e.size_bytes>=?")
            params.append(self.minimum_size)
        if self.maximum_size is not None:
            clauses.append("e.size_bytes<=?")
            params.append(self.maximum_size)
        if self.source_root_id is not None:
            clauses.append("e.source_root_id=?")
            params.append(self.source_root_id)
        if self.decision:
            clauses.append(
                "EXISTS(SELECT 1 FROM review_decisions d WHERE d.target_type='ENTRY' AND d.target_id=e.id AND d.current=1 AND d.decision=?)"
            )
            params.append(self.decision)
        if self.reason_code:
            clauses.append("c.reason_codes_json LIKE ?")
            params.append(f'%"{self.reason_code}"%')
        if self.minimum_age_timestamp is not None:
            clauses.append("e.modified_at>=?")
            params.append(self.minimum_age_timestamp)
        if self.maximum_age_timestamp is not None:
            clauses.append("e.modified_at<=?")
            params.append(self.maximum_age_timestamp)
        if self.duplicate_only:
            clauses.append(
                "EXISTS(SELECT 1 FROM exact_duplicate_members dm WHERE dm.entry_id=e.id)"
            )
        if self.project_only:
            clauses.append("EXISTS(SELECT 1 FROM projects p WHERE p.root_entry_id=e.id)")
        if self.stale is not None:
            clauses.append(
                "EXISTS(SELECT 1 FROM review_decisions d WHERE d.target_type='ENTRY' AND d.target_id=e.id AND d.current=1 AND d.stale=?)"
            )
            params.append(int(self.stale))
        if self.protected is not None:
            clauses.append(
                "c.classification=?"
                if self.protected
                else "(c.classification IS NULL OR c.classification<>'PROTECTED')"
            )
            if self.protected:
                params.append("PROTECTED")
        if self.top_level_directory:
            clauses.append("(e.relative_path=? OR e.relative_path LIKE ?)")
            params.extend((self.top_level_directory, f"{self.top_level_directory}/%"))
        return " AND ".join(clauses), tuple(params)

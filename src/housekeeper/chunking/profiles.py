"""Chunk-profile construction from configuration + persistence helpers."""

from __future__ import annotations

from .model import ChunkProfile


def profile_from_config(config, name: str | None = None) -> ChunkProfile:
    section = config.section("chunking")
    name = name or section.get("default_profile", "balanced")
    profiles = section["profiles"]
    if name not in profiles:
        raise ValueError(f"unknown chunk profile: {name}")
    spec = profiles[name]
    return ChunkProfile(
        name=name,
        algorithm="fastcdc_gear",
        algorithm_version="1",
        minimum_chunk_size=int(spec["minimum_chunk_size"]),
        average_chunk_size=int(spec["average_chunk_size"]),
        maximum_chunk_size=int(spec["maximum_chunk_size"]),
    )


def get_or_create_chunk_profile_id(database, profile: ChunkProfile) -> int:
    database.connect().execute(
        """INSERT OR IGNORE INTO chunk_profiles(name,algorithm,algorithm_version,minimum_chunk_size,average_chunk_size,maximum_chunk_size,hash_algorithm,configuration_fingerprint)
           VALUES(?,?,?,?,?,?,?,?)""",
        (
            profile.name,
            profile.algorithm,
            profile.algorithm_version,
            profile.minimum_chunk_size,
            profile.average_chunk_size,
            profile.maximum_chunk_size,
            profile.hash_algorithm,
            profile.fingerprint(),
        ),
    )
    database.connect().commit()
    row = database.fetch_one(
        "SELECT id FROM chunk_profiles WHERE name=? AND algorithm_version=? AND configuration_fingerprint=?",
        (profile.name, profile.algorithm_version, profile.fingerprint()),
    )
    assert row is not None
    return int(row["id"])

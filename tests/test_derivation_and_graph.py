"""Cross-format derivation, PDF text equivalence, and graph content-relationship projections."""

import os
import time
import zipfile

import pytest

from housekeeper.analysers.cross_format_derivation import run_cross_format_derivation_analysis
from housekeeper.analysers.normalized_content import run_normalized_content_analysis
from housekeeper.graph.builder import build_projection
from housekeeper.scanner import DriveScanner


def test_docx_pdf_derivation_is_tier6(config, database, tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    # A DOCX and a PDF with the same stem in the same directory, saved close together.
    with zipfile.ZipFile(root / "report.docx", "w") as archive:
        archive.writestr("word/document.xml", "<w:document>body</w:document>")
    (root / "report.pdf").write_bytes(b"%PDF-1.7\n stream body")
    now = time.time()
    os.utime(root / "report.docx", (now, now))
    os.utime(root / "report.pdf", (now + 120, now + 120))
    DriveScanner(database, config).scan(root, incremental=False)
    result = run_cross_format_derivation_analysis(database, config)
    assert result["pairs"] == 1
    rel = database.fetch_one(
        "SELECT relationship_type,evidence_tier,confidence FROM content_relationships WHERE relationship_type='LIKELY_EXPORT'"
    )
    assert rel is not None
    assert rel["evidence_tier"] == "TIER_6_CONTEXTUAL_INFERENCE"
    assert rel["confidence"] < 1.0  # inference, never certain
    # An editable/export inference is never a byte-identical duplicate.
    assert database.fetch_one("SELECT COUNT(*) n FROM exact_duplicate_groups")["n"] == 0


def test_unrelated_names_are_not_derivations(config, database, tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    with zipfile.ZipFile(root / "alpha.docx", "w") as archive:
        archive.writestr("word/document.xml", "<w:document>a</w:document>")
    (root / "beta.pdf").write_bytes(b"%PDF-1.7\n b")
    DriveScanner(database, config).scan(root, incremental=False)
    run_cross_format_derivation_analysis(database, config)
    assert database.fetch_all("SELECT * FROM content_relationships WHERE relationship_type='LIKELY_EXPORT'") == []


def test_pdf_text_equivalence(config, database, tmp_path):
    fitz = pytest.importorskip("fitz")
    root = tmp_path / "src"
    root.mkdir()

    def make_pdf(path, body):
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 72), body)
        doc.save(path)
        doc.close()

    make_pdf(root / "a.pdf", "The complete report text of this document goes here.")
    make_pdf(root / "b.pdf", "The complete report text of this document goes here.")  # same text, re-encoded
    make_pdf(root / "c.pdf", "An entirely different body of text for the third file.")
    DriveScanner(database, config).scan(root, incremental=False)
    run_normalized_content_analysis(database, config)
    equivalent = database.fetch_all(
        "SELECT * FROM content_relationships WHERE relationship_type='PDF_TEXT_EQUIVALENT'"
    )
    assert len(equivalent) == 1
    assert equivalent[0]["evidence_tier"] == "TIER_3_STRONG_EQUIVALENCE"


def test_graph_content_equivalence_projection(config, database, tmp_path):
    # Seed a Tier-2 and a Tier-4 relationship, then confirm each projection isolates its tier.
    database.connect().execute("INSERT INTO content_objects(hash_algorithm,full_hash,size_bytes) VALUES('sha256','a',1),('sha256','b',2),('sha256','c',3)")
    database.connect().execute(
        "INSERT INTO content_relationships(source_type,source_id,target_type,target_id,relationship_type,evidence_tier,confidence,algorithm,algorithm_version) VALUES"
        "('CONTENT_OBJECT',1,'CONTENT_OBJECT',2,'PIXEL_IDENTICAL','TIER_2_NORMALIZED_EXACT',1.0,'x','1'),"
        "('CONTENT_OBJECT',2,'CONTENT_OBJECT',3,'PARTIAL_CONTENT_OVERLAP','TIER_4_PARTIAL_OVERLAP',0.8,'y','1')"
    )
    database.connect().commit()
    equivalence = build_projection(database, "content-equivalence", max_nodes=50, max_edges=50)
    assert len(equivalence["edges"]) == 1
    assert equivalence["edges"][0]["edge_type"] == "PIXEL_IDENTICAL"
    partial = build_projection(database, "partial-overlap", max_nodes=50, max_edges=50)
    assert len(partial["edges"]) == 1
    assert partial["edges"][0]["edge_type"] == "PARTIAL_CONTENT_OVERLAP"


def test_graph_content_projection_enforces_limits(config, database, tmp_path):
    database.connect().execute(
        "INSERT INTO content_objects(hash_algorithm,full_hash,size_bytes) SELECT 'sha256', 'h'||value, value FROM (WITH RECURSIVE n(value) AS (SELECT 1 UNION ALL SELECT value+1 FROM n WHERE value<20) SELECT value FROM n)"
    )
    for i in range(1, 19):
        database.connect().execute(
            "INSERT INTO content_relationships(source_type,source_id,target_type,target_id,relationship_type,evidence_tier,confidence,algorithm,algorithm_version) VALUES('CONTENT_OBJECT',?,'CONTENT_OBJECT',?,'PIXEL_IDENTICAL','TIER_2_NORMALIZED_EXACT',1.0,'x','1')",
            (i, i + 1),
        )
    database.connect().commit()
    projection = build_projection(database, "content-equivalence", max_nodes=3, max_edges=3)
    assert len(projection["nodes"]) <= 3
    assert len(projection["edges"]) <= 3
    assert projection["truncated"] is True

from scripts.classify_pages_change import classify_entries


def test_content_only_publication_uses_fast_path() -> None:
    assert classify_entries(
        [
            ("A", "content/pulses/2026-07-23.md"),
            ("A", "public/artifacts/2026-07-23/chart/chart.svg"),
            ("M", "knowledge/curated/claims.jsonl"),
            ("M", "knowledge/curated/sources.jsonl"),
            ("M", "public-release/current.json"),
            ("M", "public-release/manifest.json"),
            ("A", "public-release/pulses/2026-07-23.md"),
            ("A", "public-release/artifacts/2026-07-23/chart/chart.csv"),
        ]
    ) == "content"


def test_code_configuration_deletions_and_renames_force_full_path() -> None:
    assert classify_entries([("M", "research_pipeline/daily.py")]) == "full"
    assert classify_entries([("M", "config/pulse.yaml")]) == "full"
    assert classify_entries([("D", "content/pulses/2026-07-23.md")]) == "full"
    assert classify_entries([("R100", "content/pulses/old.md")]) == "full"
    assert classify_entries([]) == "full"


def test_similar_but_unapproved_paths_force_full_path() -> None:
    assert classify_entries([("A", "content/pulses/latest.md")]) == "full"
    assert classify_entries([("A", "public/artifacts/2026-07-23/../secret")]) == "full"
    assert classify_entries([("A", "public-release/knowledge/private.jsonl")]) == "full"

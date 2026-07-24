from research_pipeline.pulse_identity import indexed_pulse_path, parse_pulse_path


def test_legacy_and_indexed_pulse_paths_have_stable_one_based_identity() -> None:
    legacy = parse_pulse_path("content/pulses/2026-07-24.md")
    indexed = parse_pulse_path("content/pulses/2026-07-24-2.md")

    assert legacy is not None
    assert (legacy.date, legacy.index, legacy.legacy, legacy.pulse_id) == (
        "2026-07-24",
        1,
        True,
        "pulse-2026-07-24",
    )
    assert indexed is not None
    assert (indexed.date, indexed.index, indexed.legacy, indexed.pulse_id) == (
        "2026-07-24",
        2,
        False,
        "pulse-2026-07-24-2",
    )
    assert indexed_pulse_path("2026-07-24", 3) == (
        "content/pulses/2026-07-24-3.md"
    )


def test_pulse_path_parser_rejects_zero_padded_or_unbounded_indices() -> None:
    assert parse_pulse_path("content/pulses/2026-07-24-0.md") is None
    assert parse_pulse_path("content/pulses/2026-07-24-02.md") is None
    assert parse_pulse_path("content/pulses/2026-07-24-10000.md") is None
    assert parse_pulse_path("content/pulses/2026-07-24-2.md/escape") is None

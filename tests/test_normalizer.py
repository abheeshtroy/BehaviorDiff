"""Unit tests for the normalizer.

All normalization is a pure function over already-captured data, so these
tests build NormalizeConfig and raw observation dicts/lists directly — no
live app or database is needed.
"""

from __future__ import annotations

from engine.manifest import NormalizeConfig
from engine.normalizer import Normalizer, detect_instability, normalize_rows


def _config(
    ignore_fields: list[str] | None = None,
    uuid_fields: list[str] | None = None,
    numeric_tolerance: float = 0.0,
    ignore_row_order: list[str] | None = None,
) -> NormalizeConfig:
    return NormalizeConfig(
        ignore_fields=ignore_fields or [],
        uuid_fields=uuid_fields or [],
        numeric_tolerance=numeric_tolerance,
        ignore_row_order=ignore_row_order or [],
    )


# -- field ignoring -----------------------------------------------------------


def test_ignore_fields_wildcard_matches_nested_field() -> None:
    normalizer = Normalizer(_config(ignore_fields=["*.created_at"]))
    data = {"order": {"id": 1, "created_at": "2026-01-01T00:00:00Z"}}

    normalized, report = normalizer.normalize(data)

    assert normalized == {"order": {"id": 1}}
    assert report.ignored_fields == ["order.created_at"]


def test_ignore_fields_wildcard_also_matches_top_level_field() -> None:
    normalizer = Normalizer(_config(ignore_fields=["*.created_at"]))
    data = {"created_at": "2026-01-01T00:00:00Z", "status": "ok"}

    normalized, report = normalizer.normalize(data)

    assert normalized == {"status": "ok"}
    assert report.ignored_fields == ["created_at"]


def test_ignore_fields_exact_match_does_not_affect_other_fields() -> None:
    normalizer = Normalizer(_config(ignore_fields=["cart_id"]))
    data = {"cart_id": "abc", "order_id": "xyz"}

    normalized, report = normalizer.normalize(data)

    assert normalized == {"order_id": "xyz"}
    assert report.ignored_fields == ["cart_id"]


def test_ignore_fields_exact_nested_path_only_matches_that_path() -> None:
    normalizer = Normalizer(_config(ignore_fields=["user.profile.bio"]))
    data = {
        "user": {"profile": {"bio": "hello", "name": "Ada"}},
        "other": {"bio": "should stay"},
    }

    normalized, report = normalizer.normalize(data)

    assert normalized == {
        "user": {"profile": {"name": "Ada"}},
        "other": {"bio": "should stay"},
    }
    assert report.ignored_fields == ["user.profile.bio"]


def test_ignore_fields_matches_inside_list_items() -> None:
    normalizer = Normalizer(_config(ignore_fields=["*.id"]))
    data = {"items": [{"id": 1, "sku": "A"}, {"id": 2, "sku": "B"}]}

    normalized, report = normalizer.normalize(data)

    assert normalized == {"items": [{"sku": "A"}, {"sku": "B"}]}
    assert report.ignored_fields == ["items[0].id", "items[1].id"]


# -- UUID remapping -----------------------------------------------------------


def test_uuid_auto_detected_and_replaced_with_placeholder() -> None:
    normalizer = Normalizer(_config())
    data = {"order_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6"}

    normalized, report = normalizer.normalize(data)

    assert normalized == {"order_id": "uuid-0"}
    assert report.remapped_uuids == {"3fa85f64-5717-4562-b3fc-2c963f66afa6": "uuid-0"}


def test_same_uuid_maps_to_same_placeholder_within_a_run() -> None:
    normalizer = Normalizer(_config())
    uuid = "3fa85f64-5717-4562-b3fc-2c963f66afa6"
    data = {"a": uuid, "b": uuid}

    normalized, _ = normalizer.normalize(data)

    assert normalized == {"a": "uuid-0", "b": "uuid-0"}


def test_different_uuids_map_to_different_placeholders() -> None:
    normalizer = Normalizer(_config())
    data = {
        "a": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
        "b": "11111111-1111-1111-1111-111111111111",
    }

    normalized, _ = normalizer.normalize(data)

    assert normalized == {"a": "uuid-0", "b": "uuid-1"}


def test_uuid_placeholders_stay_stable_across_separate_normalize_calls() -> None:
    normalizer = Normalizer(_config())
    uuid = "3fa85f64-5717-4562-b3fc-2c963f66afa6"

    first, _ = normalizer.normalize({"a": uuid})
    second, _ = normalizer.normalize({"b": uuid, "c": "11111111-1111-1111-1111-111111111111"})

    assert first == {"a": "uuid-0"}
    assert second == {"b": "uuid-0", "c": "uuid-1"}


def test_reset_clears_uuid_map_between_runs() -> None:
    normalizer = Normalizer(_config())
    uuid = "3fa85f64-5717-4562-b3fc-2c963f66afa6"

    normalizer.normalize({"a": uuid})
    normalizer.reset()
    second, _ = normalizer.normalize({"a": uuid})

    assert second == {"a": "uuid-0"}


def test_uuid_fields_config_forces_remap_of_non_uuid_looking_value() -> None:
    normalizer = Normalizer(_config(uuid_fields=["*.cart_id"]))
    data = {"cart_id": "not-actually-a-uuid"}

    normalized, report = normalizer.normalize(data)

    assert normalized == {"cart_id": "uuid-0"}
    assert report.remapped_uuids == {"not-actually-a-uuid": "uuid-0"}


def test_non_uuid_field_not_declared_is_left_untouched() -> None:
    normalizer = Normalizer(_config())
    data = {"sku": "SHOE-42"}

    normalized, report = normalizer.normalize(data)

    assert normalized == {"sku": "SHOE-42"}
    assert report.remapped_uuids == {}


# -- numeric tolerance ---------------------------------------------------------


def test_numeric_tolerance_rounds_to_implied_precision() -> None:
    normalizer = Normalizer(_config(numeric_tolerance=0.001))
    data = {"total": 19.999951}

    normalized, report = normalizer.normalize(data)

    assert normalized == {"total": 20.0}
    assert report.tolerance_suppressed == ["total"]


def test_numeric_tolerance_zero_disables_rounding() -> None:
    normalizer = Normalizer(_config(numeric_tolerance=0.0))
    data = {"total": 19.999951}

    normalized, report = normalizer.normalize(data)

    assert normalized == {"total": 19.999951}
    assert report.tolerance_suppressed == []


def test_numeric_tolerance_leaves_already_precise_values_unreported() -> None:
    normalizer = Normalizer(_config(numeric_tolerance=0.001))
    data = {"total": 20.0}

    normalized, report = normalizer.normalize(data)

    assert normalized == {"total": 20.0}
    assert report.tolerance_suppressed == []


# -- row ordering ---------------------------------------------------------------


def test_normalize_rows_sorts_rows_for_configured_tables() -> None:
    config = _config(ignore_row_order=["carts"])
    rows = [{"id": 2, "sku": "B"}, {"id": 1, "sku": "A"}]

    sorted_rows = normalize_rows("carts", rows, config)

    assert sorted_rows == [{"id": 1, "sku": "A"}, {"id": 2, "sku": "B"}]


def test_normalize_rows_leaves_unconfigured_tables_in_original_order() -> None:
    config = _config(ignore_row_order=["carts"])
    rows = [{"id": 2, "sku": "B"}, {"id": 1, "sku": "A"}]

    result = normalize_rows("orders", rows, config)

    assert result == rows


def test_normalize_rows_produces_stable_order_regardless_of_input_order() -> None:
    config = _config(ignore_row_order=["carts"])
    rows_a = [{"id": 1, "sku": "A"}, {"id": 2, "sku": "B"}]
    rows_b = [{"id": 2, "sku": "B"}, {"id": 1, "sku": "A"}]

    assert normalize_rows("carts", rows_a, config) == normalize_rows("carts", rows_b, config)


# -- instability detection -------------------------------------------------------


def test_detect_instability_finds_varying_field() -> None:
    config = _config()
    runs = [
        {"status": "ok", "latency_ms": 12},
        {"status": "ok", "latency_ms": 47},
        {"status": "ok", "latency_ms": 9},
    ]

    unstable = detect_instability(runs, config)

    assert unstable == {"latency_ms"}


def test_detect_instability_identical_runs_returns_empty_set() -> None:
    config = _config()
    runs = [{"status": "ok", "total": 20}, {"status": "ok", "total": 20}]

    unstable = detect_instability(runs, config)

    assert unstable == set()


def test_detect_instability_normalizes_uuids_before_comparing() -> None:
    config = _config()
    runs = [
        {"order_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6", "status": "ok"},
        {"order_id": "11111111-1111-1111-1111-111111111111", "status": "ok"},
    ]

    unstable = detect_instability(runs, config)

    assert unstable == set()


def test_detect_instability_field_missing_in_some_runs_is_unstable() -> None:
    config = _config()
    runs = [{"status": "ok", "extra": "present"}, {"status": "ok"}]

    unstable = detect_instability(runs, config)

    assert unstable == {"extra"}


def test_detect_instability_applies_ignore_and_tolerance_before_comparing() -> None:
    config = _config(ignore_fields=["*.created_at"], numeric_tolerance=0.001)
    runs = [
        {"created_at": "t1", "total": 19.9999},
        {"created_at": "t2", "total": 20.0001},
    ]

    unstable = detect_instability(runs, config)

    assert unstable == set()


def test_detect_instability_single_run_returns_empty_set() -> None:
    config = _config()

    assert detect_instability([{"status": "ok"}], config) == set()

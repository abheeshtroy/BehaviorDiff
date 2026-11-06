"""Unit tests for the structural diff.

changed_paths() is a pure function over already-captured JSON-like values, so
these tests build dicts/lists/scalars directly — no app, database, or HTTP
client is involved.
"""

from __future__ import annotations

from engine.structure import changed_paths, describe_paths


# -- locating leaves ------------------------------------------------------------


def test_identical_values_have_no_paths() -> None:
    assert changed_paths({"a": 1, "b": [1, 2]}, {"a": 1, "b": [1, 2]}) == []


def test_changed_scalar_is_named_by_its_path() -> None:
    assert changed_paths({"total": 1999, "status": "ok"}, {"total": 1799, "status": "ok"}) == ["total"]


def test_nested_object_path_is_dot_joined() -> None:
    base = {"order": {"customer": {"name": "Ada"}}}
    target = {"order": {"customer": {"name": "Grace"}}}

    assert changed_paths(base, target) == ["order.customer.name"]


def test_list_item_path_carries_its_index() -> None:
    base = {"items": [{"sku": "SHOE-42", "qty": 1}, {"sku": "HAT-1", "qty": 2}]}
    target = {"items": [{"sku": "SHOE-42", "qty": 1}, {"sku": "HAT-1", "qty": 5}]}

    assert changed_paths(base, target) == ["items[1].qty"]


def test_added_and_removed_keys_are_reported_at_the_key() -> None:
    assert changed_paths({"a": 1}, {"a": 1, "b": 2}) == ["b"]
    assert changed_paths({"a": 1, "b": 2}, {"a": 1}) == ["b"]


def test_added_and_removed_list_items_are_reported_at_the_index() -> None:
    assert changed_paths({"tags": ["a"]}, {"tags": ["a", "b"]}) == ["tags[1]"]
    assert changed_paths({"tags": ["a", "b"]}, {"tags": ["a"]}) == ["tags[1]"]


def test_a_whole_value_that_changed_type_is_the_root_path() -> None:
    # No field to name: the body used to be an object and is now an array.
    assert changed_paths({"a": 1}, [1]) == [""]


def test_root_scalars_differ_at_the_root_path() -> None:
    assert changed_paths("old", "new") == [""]
    assert changed_paths("same", "same") == []


def test_paths_are_ordered_deterministically() -> None:
    base = {"b": 1, "a": 2, "c": [3, 4]}
    target = {"b": 9, "a": 8, "c": [7, 4]}

    assert changed_paths(base, target) == ["a", "b", "c[0]"]
    assert changed_paths(target, base) == ["a", "b", "c[0]"]


def test_limit_caps_the_walk() -> None:
    base = {f"k{index}": 0 for index in range(20)}
    target = {f"k{index}": 1 for index in range(20)}

    assert len(changed_paths(base, target, limit=5)) == 5
    assert len(changed_paths(base, target, limit=None)) == 20
    assert changed_paths(base, target, limit=0) == []


def test_empty_containers_are_treated_as_values() -> None:
    assert changed_paths({"a": []}, {"a": {}}) == ["a"]
    assert changed_paths({"a": []}, {"a": []}) == []


# -- rendering -------------------------------------------------------------------


def test_describe_paths_renders_a_short_list() -> None:
    assert describe_paths(["total"]) == "total"
    assert describe_paths(["a", "b"]) == "a, b"


def test_describe_paths_counts_what_it_truncated() -> None:
    assert describe_paths(["a", "b", "c", "d", "e"]) == "a, b, c (+2 more)"


def test_describe_paths_says_so_when_there_is_no_field_to_name() -> None:
    assert describe_paths([""]) == "the whole value"
    assert describe_paths([]) == ""

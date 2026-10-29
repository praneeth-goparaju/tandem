from __future__ import annotations

import pytest

from src.gateway.validation import SchemaValidationError, validate_arguments


def test_required_type_enum_and_numeric_bounds() -> None:
    schema = {
        "type": "object",
        "properties": {
            "provider": {"type": "string", "enum": ["gmail", "icloud"]},
            "limit": {"type": "integer", "minimum": 1, "maximum": 10},
        },
        "required": ["provider", "limit"],
    }

    validate_arguments({"provider": "gmail", "limit": 3}, schema)

    with pytest.raises(SchemaValidationError, match="provider is required"):
        validate_arguments({"limit": 3}, schema)
    with pytest.raises(SchemaValidationError, match="enum"):
        validate_arguments({"provider": "other", "limit": 3}, schema)
    with pytest.raises(SchemaValidationError, match="maximum"):
        validate_arguments({"provider": "gmail", "limit": 11}, schema)


def test_array_items_and_any_of() -> None:
    schema = {
        "type": "object",
        "properties": {
            "values": {"type": "array", "items": {"type": "string"}},
            "thread_id": {"type": "string"},
            "contains": {"type": "string"},
        },
        "required": ["values"],
        "anyOf": [{"required": ["thread_id"]}, {"required": ["contains"]}],
    }

    validate_arguments({"values": ["a"], "thread_id": "t1"}, schema)

    with pytest.raises(SchemaValidationError, match="allowed shape"):
        validate_arguments({"values": ["a"]}, schema)
    with pytest.raises(SchemaValidationError, match=r"values\[0\] must be string"):
        validate_arguments({"values": [1], "contains": "x"}, schema)


def test_boolean_is_not_an_integer() -> None:
    with pytest.raises(SchemaValidationError, match="integer"):
        validate_arguments(True, {"type": "integer"})


def test_string_bounds_pattern_and_closed_object() -> None:
    schema = {
        "type": "object",
        "properties": {
            "area": {
                "type": "string",
                "minLength": 1,
                "maxLength": 8,
                "pattern": r"^[a-z_]+$",
            },
            "message": {"type": "string", "maxLength": 10},
        },
        "required": ["area", "message"],
        "additionalProperties": False,
    }

    validate_arguments({"area": "health", "message": "hello"}, schema)

    with pytest.raises(SchemaValidationError, match="pattern"):
        validate_arguments({"area": "C123", "message": "hello"}, schema)
    with pytest.raises(SchemaValidationError, match="maxLength"):
        validate_arguments({"area": "health", "message": "x" * 11}, schema)
    with pytest.raises(SchemaValidationError, match="allowed property"):
        validate_arguments({"area": "health", "message": "hello", "channel": "C123"}, schema)


def test_array_size_and_uniqueness() -> None:
    schema = {
        "type": "array",
        "minItems": 1,
        "maxItems": 2,
        "uniqueItems": True,
        "items": {"type": "string"},
    }

    validate_arguments(["a", "b"], schema)

    with pytest.raises(SchemaValidationError, match="minItems"):
        validate_arguments([], schema)
    with pytest.raises(SchemaValidationError, match="maxItems"):
        validate_arguments(["a", "b", "c"], schema)
    with pytest.raises(SchemaValidationError, match="unique"):
        validate_arguments(["a", "a"], schema)


def test_unknown_schema_keyword_fails_closed_even_on_unused_property() -> None:
    schema = {
        "type": "object",
        "properties": {
            "email": {"type": "string", "format": "email"},
        },
    }

    with pytest.raises(SchemaValidationError, match="unsupported keyword: format"):
        validate_arguments({}, schema)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_numbers_are_rejected_at_any_depth(value: float) -> None:
    with pytest.raises(SchemaValidationError, match="NaN or infinity"):
        validate_arguments({"nested": [value]}, {"type": "object"})


def test_invalid_schema_constraints_fail_closed() -> None:
    with pytest.raises(SchemaValidationError, match="non-negative integer"):
        validate_arguments("x", {"type": "string", "maxLength": -1})
    with pytest.raises(SchemaValidationError, match="finite number"):
        validate_arguments(1, {"type": "number", "maximum": float("inf")})

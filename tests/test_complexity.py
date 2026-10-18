"""Declaration guard and schema-independent recursive cost regressions."""

import asyncio
import math
from unittest.mock import Mock

import pytest
import strawberry
import strawberry.schema.schema as strawberry_schema
from graphql import parse

from mlb_statsapi_graphql import complexity
from mlb_statsapi_graphql.complexity import QueryCostLimiter, estimate_cost
from mlb_statsapi_graphql.costs import priced, unpriced_resolvers
from mlb_statsapi_graphql.schema import schema


@strawberry.type
class Leaf:
    @strawberry.field(metadata=priced(
        5, request_key=lambda parent, args: ("lookup", parent, args["season"])
        if parent is not None else None,
    ))
    def value(self, season: int = 2026) -> int:
        return season

    @strawberry.field(metadata=priced(
        2, request_key=lambda parent, args: ("constant", args["id"]),
    ))
    def constant(self, id: int = 1) -> int:
        return id


@strawberry.type
class Wrapper:
    leaf: Leaf = strawberry.field(metadata=priced(0, result_key=lambda parent, args: parent))


@strawberry.type
class CostQuery:
    @strawberry.field(metadata=priced(
        3, request_key=lambda parent, args: ("wrapper", args["id"]),
        result_key=lambda parent, args: args["id"],
    ))
    def wrapper(self, id: int = 7) -> Wrapper:
        return Wrapper(leaf=Leaf())

    @strawberry.field(metadata=priced(0))
    def unknown(self) -> Wrapper:
        return Wrapper(leaf=Leaf())

    @strawberry.field(metadata=priced(0, max_items=4))
    def bounded(self) -> list[Wrapper]:
        return []

    @strawberry.field(metadata=priced(0, max_items=3))
    def matrix(self) -> list[list[Wrapper]]:
        return []

    @strawberry.field(metadata=priced(0, max_items=0))
    def empty(self) -> list[Wrapper]:
        return []

    @strawberry.field(metadata=priced(0))
    def unbounded(self) -> list[Wrapper]:
        return []


fixture_schema = strawberry.Schema(query=CostQuery, extensions=[lambda: QueryCostLimiter(100)])


def cost(query, variables=None):
    return estimate_cost(fixture_schema._schema, parse(query), None, variables)


def test_guard_covers_all_production_resolvers():
    assert unpriced_resolvers(schema._schema) == []
    assert unpriced_resolvers(fixture_schema._schema) == []


def test_guard_detects_unpriced_resolver():
    @strawberry.type
    class Unpriced:
        @strawberry.field
        def value(self) -> int:
            return 1

    assert unpriced_resolvers(strawberry.Schema(query=Unpriced)._schema) == ["Unpriced.value"]


@pytest.mark.parametrize("query, expected", [
    ("{ wrapper { leaf { value } } }", 8),
    ("{ a: wrapper { leaf { value } } b: wrapper { leaf { value } } }", 8),
    ("{ wrapper { leaf { a: value b: value(season: 2025) } } }", 13),
    ("{ a: wrapper(id: 1) { leaf { value } } b: wrapper(id: 2) { leaf { value } } }", 16),
    ("{ a: unknown { leaf { value } } b: unknown { leaf { value } } }", 10),
    ("{ bounded { leaf { value } } }", 20),
    ("{ matrix { leaf { value } } }", 45),
    ("{ empty { leaf { value } } }", 0),
    ("{ unbounded { leaf { constant } } }", 2),
    ("{ bounded { leaf { a: value b: value } } }", 40),
    ("{ bounded { leaf { a: constant b: constant } } }", 2),
    ("{ unbounded { leaf { __typename } } }", 0),
    ("{ unknown { leaf { __typename } } }", 0),
    ("{ wrapper { ...F ...F } } fragment F on Wrapper { leaf { value } }", 8),
    ("{ unknown { leaf { value value } } }", 5),
    ("{ unbounded { leaf { value @skip(if: true) } } }", 0),
    ("query($s: Int = 2025) { wrapper { leaf { value(season: $s) } } }", 8),
])
def test_recursive_costs(query, expected):
    assert cost(query) == expected


def test_unbounded_list_rejected_before_execution():
    query = "{ unbounded { leaf { value } } }"
    assert math.isinf(cost(query))
    result = fixture_schema.execute_sync(query)
    assert result.data is None
    assert result.errors[0].extensions["code"] == "QUERY_COMPLEXITY_LIMIT"


@pytest.mark.parametrize("query, variables", [
    ("{ missing }", None),
    ('{ wrapper { leaf { value(season: "invalid") } } }', None),
    ("{ wrapper { ...F } } fragment F on Wrapper { ...F }", None),
    ("query($s: Int!) { wrapper { leaf { value(season: $s) } } }", None),
    ("query($s: Int!) { wrapper { leaf { value(season: $s) } } }", {"s": "invalid"}),
    ("query($skip: Boolean!) { wrapper @skip(if: $skip) { leaf { value } } }",
     {"skip": "invalid"}),
])
def test_invalid_operation(query, variables):
    assert cost(query, variables) is None


@pytest.mark.parametrize("execute_async", [False, True])
@pytest.mark.parametrize("query, rejected", [
    ("{ wrapper { leaf { value } } }", False),
    ("{ unbounded { leaf { value } } }", True),
])
def test_request_validates_document_once(monkeypatch, execute_async, query, rejected):
    validate = Mock(wraps=complexity.validate)
    monkeypatch.setattr(strawberry_schema, "validate", validate)
    monkeypatch.setattr(complexity, "validate", validate)

    result = (asyncio.run(fixture_schema.execute(query)) if execute_async
              else fixture_schema.execute_sync(query))

    if rejected:
        assert result.data is None
        assert result.errors[0].extensions["code"] == "QUERY_COMPLEXITY_LIMIT"
    else:
        assert result.errors is None
        assert result.data == {"wrapper": {"leaf": {"value": 2026}}}
    assert validate.call_count == 1

    # Standalone callers still validate, then use the same cost traversal.
    assert cost(query) == (math.inf if rejected else 8)
    assert validate.call_count == 2


def test_priced_defaults():
    declaration = priced(0)["cost"]
    assert declaration.weight == 0
    assert declaration.request_key(7, {}) is None
    assert declaration.result_key(7, {}) is None
    assert declaration.max_items is None


@pytest.mark.parametrize("weight, max_items", [(-1, None), (0, -1)])
def test_priced_rejects_negative_cost_or_bound(weight, max_items):
    with pytest.raises(ValueError, match="Costs and cardinality bounds must be nonnegative"):
        priced(weight, max_items=max_items)

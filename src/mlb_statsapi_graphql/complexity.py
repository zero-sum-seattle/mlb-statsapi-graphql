"""Estimate the upstream cost of an operation and reject it before execution.

The estimate is specific to this small schema, not a general cost engine:

- Each distinct root ``team(id:)`` or ``player(id:)`` costs ROOT_FIELD_COSTS.
- Each distinct team whose ``roster`` is selected adds ROSTER_COST.
- Each distinct (team ID, season) whose ``stats`` is selected adds TEAM_STATS_COST.
- Every other field (scalars, roster entries, introspection) costs 0, because
  it reads data that an earlier upstream call already returned.

"Distinct" mirrors the request-scoped DataLoaders: aliases that load the same
ID share one upstream call, so they are charged once. Only the operation that
will execute is walked, honouring fragments, @skip/@include, and variables.
When an ID cannot be known before execution it is charged as a new entity.
"""

from collections.abc import Iterator, Mapping
from typing import Any

from graphql import (
    DocumentNode,
    FieldNode,
    FragmentDefinitionNode,
    FragmentSpreadNode,
    GraphQLBoolean,
    GraphQLError,
    GraphQLInt,
    GraphQLSchema,
    InlineFragmentNode,
    OperationType,
    SelectionSetNode,
    get_operation_ast,
    value_from_ast,
)
from graphql import ExecutionResult as GraphQLExecutionResult
from graphql.execution import get_variable_values
from strawberry.extensions import SchemaExtension

ROOT_FIELD_COSTS = {"team": 5, "player": 5}
ROSTER_COST = 10
TEAM_STATS_COST = 5

COMPLEXITY_ERROR_MESSAGE = "Query cost exceeds the configured limit."


def complexity_error() -> GraphQLError:
    return GraphQLError(COMPLEXITY_ERROR_MESSAGE, extensions={"code": "QUERY_COMPLEXITY_LIMIT"})


def estimate_cost(
    schema: GraphQLSchema,
    document: DocumentNode,
    operation_name: str | None,
    raw_variables: Mapping[str, Any] | None,
) -> int | None:
    """Return the estimated cost, or None when the operation cannot execute.

    None covers a missing or ambiguous operation and invalid variables. In
    those cases graphql-core reports an error before running any resolver.
    """
    operation = get_operation_ast(document, operation_name)
    if operation is None or operation.operation != OperationType.QUERY:
        return None
    coerced = get_variable_values(
        schema, operation.variable_definitions or (), dict(raw_variables or {})
    )
    if isinstance(coerced, list):  # a list of variable coercion errors
        return None
    # graphql-core 3.3 wraps coerced values; older versions return a plain dict.
    variables = getattr(coerced, "coerced", coerced)

    fragments = {
        definition.name.value: definition
        for definition in document.definitions
        if isinstance(definition, FragmentDefinitionNode)
    }
    entities: set[tuple[str, object]] = set()
    rosters: set[object] = set()
    team_stats: set[tuple[object, object]] = set()
    for field in _included_fields(operation.selection_set, fragments, variables):
        name = field.name.value
        if name not in ROOT_FIELD_COSTS:
            continue
        id = _id_argument(field, variables)
        entities.add((name, id))
        if name == "team":
            for subfield in _included_fields(field.selection_set, fragments, variables):
                if subfield.name.value == "roster":
                    rosters.add(id)
                elif subfield.name.value == "stats":
                    team_stats.add((id, _int_argument(subfield, variables, "season")))

    return (
        sum(ROOT_FIELD_COSTS[name] for name, _ in entities)
        + ROSTER_COST * len(rosters)
        + TEAM_STATS_COST * len(team_stats)
    )


def _included_fields(
    selection_set: SelectionSetNode | None,
    fragments: Mapping[str, FragmentDefinitionNode],
    variables: Mapping[str, Any],
    visited: frozenset[str] = frozenset(),
) -> Iterator[FieldNode]:
    """Yield the fields that execution would run, flattening fragments.

    This schema has no interfaces or unions, so validation guarantees every
    fragment's type condition matches and type conditions can be ignored.
    """
    if selection_set is None:
        return
    for selection in selection_set.selections:
        if not _is_included(selection, variables):
            continue
        if isinstance(selection, FieldNode):
            yield selection
        elif isinstance(selection, InlineFragmentNode):
            yield from _included_fields(selection.selection_set, fragments, variables, visited)
        elif isinstance(selection, FragmentSpreadNode):
            name = selection.name.value
            # Validation already rejects fragment cycles; this is a backstop.
            if name in visited or name not in fragments:
                continue
            yield from _included_fields(
                fragments[name].selection_set, fragments, variables, visited | {name}
            )


def _is_included(
    node: FieldNode | InlineFragmentNode | FragmentSpreadNode, variables: Mapping[str, Any]
) -> bool:
    """Apply @skip(if:) and @include(if:) the way execution does."""
    for directive in node.directives or ():
        name = directive.name.value
        if name not in ("skip", "include"):
            continue
        condition = next(
            (arg.value for arg in directive.arguments if arg.name.value == "if"), None
        )
        value = value_from_ast(condition, GraphQLBoolean, variables) if condition else None
        if not isinstance(value, bool):
            continue  # Unknown: keep the field, which can only overestimate.
        if (name == "skip") == value:
            return False
    return True


def _id_argument(field: FieldNode, variables: Mapping[str, Any]) -> object:
    return _int_argument(field, variables, "id")


def _int_argument(field: FieldNode, variables: Mapping[str, Any], name: str) -> object:
    for arg in field.arguments or ():
        if arg.name.value == name:
            value = value_from_ast(arg.value, GraphQLInt, variables)
            if isinstance(value, int):
                return value
    # Unknown argument: a fresh object never equals another key, so it is charged.
    return object()


class QueryCostLimiter(SchemaExtension):
    """Reject operations whose estimated cost exceeds max_cost.

    Runs after parsing and validation and before any resolver. Setting
    execution_context.result makes Strawberry skip execution entirely, so a
    rejected operation never reaches a DataLoader or the MLB client.
    """

    def __init__(self, max_cost: int) -> None:
        self.max_cost = max_cost

    def on_execute(self) -> Iterator[None]:
        context = self.execution_context
        assert context.graphql_document is not None
        cost = estimate_cost(
            # Strawberry exposes the underlying graphql-core schema as _schema.
            context.schema._schema,
            context.graphql_document,
            context.operation_name,
            context.variables,
        )
        if cost is not None and cost > self.max_cost:
            context.result = GraphQLExecutionResult(data=None, errors=[complexity_error()])
        yield

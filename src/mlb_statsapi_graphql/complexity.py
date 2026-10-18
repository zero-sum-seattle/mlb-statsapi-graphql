"""Estimate upstream work recursively from Strawberry field cost metadata.

Request identities mirror loader caches; result identities propagate separately.
Unknown parents remain distinct. Unbounded lists are free to select, but any
unknown costed requests below them are infinite and rejected by the cost limiter.
"""

import math
from collections.abc import Hashable, Iterator, Mapping
from typing import Any

from graphql import (
    DocumentNode,
    FieldNode,
    FragmentDefinitionNode,
    FragmentSpreadNode,
    GraphQLBoolean,
    GraphQLError,
    GraphQLField,
    GraphQLList,
    GraphQLNonNull,
    GraphQLObjectType,
    GraphQLOutputType,
    GraphQLSchema,
    InlineFragmentNode,
    OperationType,
    SelectionSetNode,
    get_operation_ast,
    is_abstract_type,
    is_object_type,
    validate,
    value_from_ast,
)
from graphql import ExecutionResult as GraphQLExecutionResult
from graphql.execution import get_variable_values
from graphql.execution.values import get_argument_values
from strawberry.extensions import SchemaExtension

from .costs import Cost

COMPLEXITY_ERROR_MESSAGE = "Query cost exceeds the configured limit."


def complexity_error() -> GraphQLError:
    return GraphQLError(COMPLEXITY_ERROR_MESSAGE, extensions={"code": "QUERY_COMPLEXITY_LIMIT"})


def estimate_cost(
    schema: GraphQLSchema,
    document: DocumentNode,
    operation_name: str | None,
    raw_variables: Mapping[str, Any] | None,
) -> int | float | None:
    """Return the estimated cost, or None when the operation cannot execute.

    None covers validation errors, a missing or ambiguous operation, and invalid variables. In
    those cases graphql-core reports an error before running any resolver.
    """
    operation = get_operation_ast(document, operation_name)
    if operation is None or operation.operation != OperationType.QUERY or validate(schema, document):
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
    estimate = _CostEstimate(schema, fragments, variables, coerced)
    assert schema.query_type is not None
    return estimate.selection_cost(schema.query_type, operation.selection_set, None, 1)


class _CostEstimate:
    """Per-operation state for one cost estimate.

    ``parent_key`` is the identity the parent field's ``Cost.result`` returned
    (for example a team ID), or None when it is unknown. ``count`` is how many
    distinct parents the selection may run for, multiplied by list bounds.
    """

    def __init__(
        self,
        schema: GraphQLSchema,
        fragments: Mapping[str, FragmentDefinitionNode],
        variables: Mapping[str, Any],
        coerced: Any,
    ) -> None:
        self.schema = schema
        self.fragments = fragments
        self.variables = variables
        self.coerced = coerced
        # Request keys already charged; mirrors the request-scoped loader caches.
        self.charged: set[Hashable] = set()

    def selection_cost(
        self,
        type_: GraphQLOutputType,
        selection_set: SelectionSetNode,
        parent_key: Hashable | None,
        count: float,
    ) -> float:
        """Return the cost of selecting ``selection_set`` on ``type_``."""
        if count == 0:
            return 0
        if is_abstract_type(type_):
            # Sum possible runtime branches conservatively; shared request keys
            # are still deduplicated across branches.
            return sum(
                self.selection_cost(possible, selection_set, parent_key, count)
                for possible in self.schema.get_possible_types(type_)
            )
        if not is_object_type(type_):
            return 0
        total: float = 0
        for nodes in self._fields_by_response_key(type_, selection_set):
            total += self._field_cost(type_, nodes, parent_key, count)
        return total

    def _field_cost(
        self,
        parent_type: GraphQLObjectType,
        nodes: list[FieldNode],
        parent_key: Hashable | None,
        count: float,
    ) -> float:
        """Return the cost of one response key, including its subselections."""
        name = nodes[0].name.value
        if name.startswith("__"):
            return 0
        field = parent_type.fields[name]
        cost = _declared_cost(field)
        if cost is None and _has_resolver(field):
            # Defense in depth if a schema bypasses the declaration guard.
            return math.inf
        # Plain attribute fields do no upstream work and have unknown identity.
        cost = cost or _FREE
        args = get_argument_values(field, nodes[0], self.coerced)
        total = self._charge(cost, cost.request(parent_key, args), count)

        selections = _merged_selections(nodes)
        if selections is None:
            return total
        output, child_count = _unwrap_lists(field.type, cost.max_items, count)
        # One scalar identity cannot describe distinct list elements.
        child_key = None if _is_list(field.type) else cost.result(parent_key, args)
        return total + self.selection_cost(output, selections, child_key, child_count)

    def _charge(self, cost: Cost, request_key: Hashable | None, count: float) -> float:
        """Charge a request once if its key is known, otherwise once per parent."""
        if not cost.weight:
            return 0
        if request_key is None:
            return cost.weight * count
        if request_key in self.charged:
            return 0
        self.charged.add(request_key)
        return cost.weight

    def _fields_by_response_key(
        self, type_: GraphQLObjectType, selection_set: SelectionSetNode
    ) -> Iterator[list[FieldNode]]:
        """Group included fields by alias, as execution merges them."""
        grouped: dict[str, list[FieldNode]] = {}
        for node in _included_fields(
            selection_set, self.fragments, self.variables, schema=self.schema, runtime_type=type_
        ):
            key = node.alias.value if node.alias else node.name.value
            grouped.setdefault(key, []).append(node)
        return iter(grouped.values())


_FREE = Cost(0)


def _declared_cost(field: GraphQLField) -> Cost | None:
    definition = (field.extensions or {}).get("strawberry-definition")
    cost = definition.metadata.get("cost") if definition else None
    return cost if isinstance(cost, Cost) else None


def _has_resolver(field: GraphQLField) -> bool:
    definition = (field.extensions or {}).get("strawberry-definition")
    return definition is not None and definition.base_resolver is not None


def _unwrap_lists(
    output: GraphQLOutputType, max_items: int | None, count: float
) -> tuple[GraphQLOutputType, float]:
    """Strip list and non-null wrappers, multiplying count by each list's bound.

    An unbounded list makes the count infinite. A zero bound is checked first
    so it stays zero instead of becoming ``0 * inf``.
    """
    while isinstance(output, (GraphQLNonNull, GraphQLList)):
        if isinstance(output, GraphQLList):
            if max_items == 0:
                count = 0
            elif max_items is None:
                count *= math.inf
            else:
                count *= max_items
        output = output.of_type
    return output, count


def _is_list(output: GraphQLOutputType) -> bool:
    if isinstance(output, GraphQLNonNull):
        output = output.of_type
    return isinstance(output, GraphQLList)


def _merged_selections(nodes: list[FieldNode]) -> SelectionSetNode | None:
    """Merge the subselections of every node that shares one response key."""
    selections = tuple(
        selection
        for node in nodes
        if node.selection_set
        for selection in node.selection_set.selections
    )
    return SelectionSetNode(selections=selections) if selections else None


def _included_fields(
    selection_set: SelectionSetNode | None,
    fragments: Mapping[str, FragmentDefinitionNode],
    variables: Mapping[str, Any],
    visited: frozenset[str] = frozenset(),
    *,
    schema: GraphQLSchema,
    runtime_type: Any,
) -> Iterator[FieldNode]:
    """Yield the fields that execution would run, flattening fragments.

    Type conditions are checked against each possible runtime object type.
    """
    if selection_set is None:
        return
    for selection in selection_set.selections:
        if not _is_included(selection, variables):
            continue
        if isinstance(selection, FieldNode):
            yield selection
        elif isinstance(selection, InlineFragmentNode):
            if _matches(selection.type_condition, schema, runtime_type):
                yield from _included_fields(selection.selection_set, fragments, variables, visited,
                                            schema=schema, runtime_type=runtime_type)
        elif isinstance(selection, FragmentSpreadNode):
            name = selection.name.value
            # Validation already rejects fragment cycles; this is a backstop.
            if name in visited or name not in fragments:
                continue
            if _matches(fragments[name].type_condition, schema, runtime_type):
                yield from _included_fields(
                    fragments[name].selection_set, fragments, variables, visited | {name},
                    schema=schema, runtime_type=runtime_type,
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


def _matches(condition, schema, runtime_type):
    if condition is None:
        return True
    conditional_type = schema.get_type(condition.name.value)
    return conditional_type is runtime_type or (
        is_abstract_type(conditional_type) and schema.is_sub_type(conditional_type, runtime_type)
    )


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

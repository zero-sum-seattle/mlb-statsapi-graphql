"""Estimate an operation's upstream cost from field ``Cost`` declarations.

The estimate visits the fields execution would run, collected by graphql-core
itself, and adds up each field's declared ``Cost``:

- A request with a known key (for example ``("team", 147)``) is charged once,
  mirroring the request-scoped DataLoader caches.
- A request with an unknown key is charged once per possible parent.
- A list field multiplies the possible parents below it by ``max_items``. An
  unbounded list makes any costed request below it infinite, so it is rejected.
"""

import math
from collections.abc import Hashable, Iterator, Mapping
from typing import Any

from graphql import (
    DocumentNode,
    FragmentDefinitionNode,
    GraphQLError,
    GraphQLField,
    GraphQLList,
    GraphQLNamedType,
    GraphQLNonNull,
    GraphQLObjectType,
    GraphQLOutputType,
    GraphQLSchema,
    OperationDefinitionNode,
    OperationType,
    get_named_type,
    get_operation_ast,
    is_abstract_type,
    is_object_type,
    validate,
)
from graphql import ExecutionResult as GraphQLExecutionResult
from graphql.execution import VariableValues, get_argument_values, get_variable_values
from graphql.execution.collect_fields import (
    FieldDetailsList,
    FragmentDetails,
    GroupedFieldSet,
    collect_fields,
    collect_subfields,
)
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
) -> float | None:
    """Return the estimated cost, or None when the operation cannot execute.

    None covers validation errors, a missing or ambiguous operation, and invalid variables. In
    those cases graphql-core reports an error before running any resolver.
    """
    operation = get_operation_ast(document, operation_name)
    if operation is None or operation.operation != OperationType.QUERY or validate(schema, document):
        return None
    variables = get_variable_values(
        schema, operation.variable_definitions or (), dict(raw_variables or {})
    )
    if isinstance(variables, list):  # a list of variable coercion errors
        return None
    fragments = {
        definition.name.value: FragmentDetails(definition)
        for definition in document.definitions
        if isinstance(definition, FragmentDefinitionNode)
    }
    return _CostEstimate(schema, fragments, variables, operation).total()


class _CostEstimate:
    """Per-operation state for one cost estimate.

    ``parent_key`` is the identity the parent field's ``Cost.result`` returned
    (for example a team ID), or None when it is unknown. ``count`` is how many
    distinct parents a field may run for, multiplied by list bounds.
    """

    def __init__(
        self,
        schema: GraphQLSchema,
        fragments: dict[str, FragmentDetails],
        variables: VariableValues,
        operation: OperationDefinitionNode,
    ) -> None:
        self.schema = schema
        self.fragments = fragments
        self.variables = variables
        self.operation = operation
        # Request keys already charged; mirrors the request-scoped loader caches.
        self.charged: set[Hashable] = set()

    def total(self) -> float:
        query_type = self.schema.query_type
        assert query_type is not None
        root = collect_fields(
            self.schema, self.fragments, self.variables, query_type, self.operation
        )
        return self._fields_cost(query_type, root.grouped_field_set, None, 1)

    def _fields_cost(
        self,
        type_: GraphQLObjectType,
        fields: GroupedFieldSet,
        parent_key: Hashable | None,
        count: float,
    ) -> float:
        return sum(
            self._field_cost(type_, details, parent_key, count) for details in fields.values()
        )

    def _field_cost(
        self,
        parent_type: GraphQLObjectType,
        details: FieldDetailsList,
        parent_key: Hashable | None,
        count: float,
    ) -> float:
        """Return the cost of one response key, including its subfields."""
        node = details[0].node
        if node.name.value.startswith("__"):
            return 0
        field = parent_type.fields[node.name.value]
        cost = _declared_cost(field)
        if cost is None:
            # Defense in depth if a schema bypasses the declaration guard.
            return math.inf
        args = get_argument_values(field, node, self.variables, details[0].fragment_variable_values)
        total = self._charge(cost, cost.request(parent_key, args), count)

        depth = _list_depth(field.type)
        if depth:
            # One scalar identity cannot describe distinct list elements.
            child_key, child_count = None, _bounded(count, cost.max_items, depth)
        else:
            child_key, child_count = cost.result(parent_key, args), count
        output = get_named_type(field.type)
        return total + self._subfields_cost(output, details, child_key, child_count)

    def _subfields_cost(
        self,
        type_: GraphQLNamedType,
        details: FieldDetailsList,
        parent_key: Hashable | None,
        count: float,
    ) -> float:
        if count == 0:
            return 0
        if is_abstract_type(type_):
            # Sum possible runtime branches conservatively; shared request keys
            # are still deduplicated across branches.
            return sum(
                self._subfields_cost(possible, details, parent_key, count)
                for possible in self.schema.get_possible_types(type_)
            )
        if not is_object_type(type_):
            return 0
        subfields = collect_subfields(
            self.schema, self.fragments, self.variables, self.operation, type_, details
        )
        return self._fields_cost(type_, subfields.grouped_field_set, parent_key, count)

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


_FREE = Cost(0)


def _declared_cost(field: GraphQLField) -> Cost | None:
    """Return the field's declared Cost, or None for an undeclared resolver.

    Plain attribute fields read data their parent already loaded, so they are free.
    """
    definition = (field.extensions or {}).get("strawberry-definition")
    cost = definition.metadata.get("cost") if definition else None
    if isinstance(cost, Cost):
        return cost
    if definition is not None and definition.base_resolver is not None:
        return None
    return _FREE


def _list_depth(type_: GraphQLOutputType) -> int:
    """Count list layers, so ``[[Team]]`` has depth 2."""
    depth = 0
    while isinstance(type_, (GraphQLNonNull, GraphQLList)):
        depth += isinstance(type_, GraphQLList)
        type_ = type_.of_type
    return depth


def _bounded(count: float, max_items: int | None, depth: int) -> float:
    """Multiply count by max_items once per list layer; None means unbounded."""
    if max_items == 0:
        return 0  # Checked first so an infinite count becomes 0, not 0 * inf.
    return count * (math.inf if max_items is None else max_items) ** depth


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

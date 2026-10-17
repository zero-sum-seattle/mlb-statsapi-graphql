"""Estimate upstream work recursively from Strawberry field cost metadata.

Request identities mirror loader caches; result identities propagate separately.
Unknown parents remain distinct. Unbounded lists are free to select, but any
unknown costed requests below them are infinite and rejected by the cost limiter.
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
    GraphQLList,
    GraphQLNonNull,
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
    requests: set[object] = set()

    def walk(type_, selection_set, parent, count):
        if count == 0:
            return 0
        if is_abstract_type(type_):
            # Sum possible runtime branches conservatively; shared request keys
            # are still deduplicated across branches.
            return sum(walk(t, selection_set, parent, count)
                       for t in schema.get_possible_types(type_))
        if not is_object_type(type_):
            return 0
        total = 0
        grouped = {}
        for node in _included_fields(selection_set, fragments, variables,
                                     schema=schema, runtime_type=type_):
            grouped.setdefault(node.alias.value if node.alias else node.name.value, []).append(node)
        for nodes in grouped.values():
            node = nodes[0]
            if node.name.value.startswith("__"):
                continue
            field = type_.fields[node.name.value]
            args = get_argument_values(field, node, coerced)
            definition = (field.extensions or {}).get("strawberry-definition")
            declaration = definition.metadata.get("cost") if definition else None
            result = None
            if isinstance(declaration, Cost):
                key = declaration.request(parent, args)
                if declaration.weight and (key is None or key not in requests):
                    total += declaration.weight * (count if key is None else 1)
                    if key is not None:
                        requests.add(key)
                result = declaration.result(parent, args)
            elif definition is not None and definition.base_resolver is not None:
                # Defense in depth if a schema bypasses the declaration guard.
                return float("inf")
            child_count = count
            output = field.type
            while isinstance(output, (GraphQLNonNull, GraphQLList)):
                if isinstance(output, GraphQLList):
                    bound = declaration.max_items if isinstance(declaration, Cost) else None
                    child_count = (
                        0 if bound == 0 else
                        child_count * (bound if bound is not None else float("inf"))
                    )
                    # One scalar identity cannot describe distinct list elements.
                    result = None
                output = output.of_type
            selections = tuple(selection for n in nodes if n.selection_set
                               for selection in n.selection_set.selections)
            if selections:
                total += walk(output, SelectionSetNode(selections=selections), result, child_count)
        return total

    return walk(schema.query_type, operation.selection_set, None, 1)


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

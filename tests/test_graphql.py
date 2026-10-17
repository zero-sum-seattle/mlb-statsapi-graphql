from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from mlbstatsapi import AsyncMlb
from mlbstatsapi.exceptions import (
    MlbDecodeError, MlbHttpError, MlbTimeoutError, MlbTransportError,
)
from mlbstatsapi.models.people import Person, Player
from pydantic import ValidationError
from mlbstatsapi.models.teams import Team

from mlb_statsapi_graphql.app import create_app


@pytest.fixture
def mlb():
    client = AsyncMock(spec=AsyncMlb)
    client.__aenter__.return_value = client
    client.get_team.return_value = Team(
        id=136, link="/api/v1/teams/136", name="Seattle Mariners", abbreviation="SEA"
    )
    return client


@pytest.fixture
def api(mlb):
    with TestClient(create_app(mlb_factory=lambda: mlb)) as client:
        yield client
    mlb.__aenter__.assert_awaited_once()
    mlb.__aexit__.assert_awaited_once()


def test_successful_team_lookup(api, mlb):
    response = api.post("/graphql", json={"query": "{ team(id: 136) { id name abbreviation } }"})
    assert response.status_code == 200
    assert response.json() == {"data": {"team": {
        "id": 136, "name": "Seattle Mariners", "abbreviation": "SEA"
    }}}
    mlb.get_team.assert_awaited_once_with(136)


def test_team_not_found(api, mlb):
    mlb.get_team.return_value = None
    response = api.post("/graphql", json={"query": "{ team(id: 999999) { id } }"})
    assert response.json() == {"data": {"team": None}}
    mlb.get_team.assert_awaited_once_with(999999)


def test_field_selection(api):
    response = api.post("/graphql", json={"query": "{ team(id: 136) { abbreviation } }"})
    assert response.json() == {"data": {"team": {"abbreviation": "SEA"}}}


@pytest.mark.parametrize("error", [
    MlbTimeoutError("private timeout detail"),
    MlbTransportError("private transport detail"),
    MlbHttpError(503, "private response detail"),
    MlbDecodeError("private payload detail"),
])
def test_upstream_error(api, mlb, error):
    mlb.get_team.side_effect = error
    response = api.post("/graphql", json={"query": "{ team(id: 136) { id } }"})
    assert response.status_code == 200
    body = response.json()
    assert body["data"] == {"team": None}
    assert len(body["errors"]) == 1
    assert body["errors"][0]["message"] == (
        "The MLB service is currently unavailable. Please try again later."
    )
    assert body["errors"][0]["extensions"] == {"code": "UPSTREAM_ERROR"}
    assert body["errors"][0]["path"] == ["team"]
    assert "private" not in response.text


def test_optional_upstream_attributes(api, mlb):
    mlb.get_team.return_value = Team(id=136, link="/api/v1/teams/136")
    response = api.post("/graphql", json={"query": "{ team(id: 136) { id name abbreviation } }"})
    assert response.json() == {"data": {"team": {
        "id": 136, "name": None, "abbreviation": None
    }}}


def test_invalid_field_is_rejected_without_upstream_call(api, mlb):
    response = api.post("/graphql", json={"query": "{ team(id: 136) { unknownField } }"})
    assert response.json()["errors"]
    mlb.get_team.assert_not_awaited()


def test_graphql_ide(api):
    response = api.get("/graphql", headers={"accept": "text/html"})
    assert response.status_code == 200
    assert "graphiql" in response.text.lower()


ROSTER_QUERY = """{ team(id: 136) {
    id name roster { player { id fullName } position { abbreviation } }
} }"""


@pytest.fixture
def roster_player():
    return Player(
        id=605141, link="/api/v1/people/605141", fullName="Mookie Betts",
        jerseyNumber="50", status={"code": "A", "description": "Active"},
        position={"code": "6", "name": "Shortstop", "type": "Infielder", "abbreviation": "SS"},
    )


def test_player_lookup(api, mlb):
    mlb.get_person.return_value = Person(
        id=605141, link="/api/v1/people/605141", fullName="Mookie Betts"
    )
    response = api.post("/graphql", json={"query": "{ player(id: 605141) { id fullName } }"})
    assert response.json() == {"data": {"player": {"id": 605141, "fullName": "Mookie Betts"}}}
    mlb.get_person.assert_awaited_once_with(605141)
    mlb.get_team.assert_not_awaited()
    mlb.get_team_roster.assert_not_awaited()


def test_player_not_found(api, mlb):
    mlb.get_person.return_value = None
    response = api.post("/graphql", json={"query": "{ player(id: 999999) { id } }"})
    assert response.json() == {"data": {"player": None}}
    mlb.get_person.assert_awaited_once_with(999999)


def test_player_optional_name(api, mlb):
    mlb.get_person.return_value = Person(id=605141, link="/api/v1/people/605141")
    response = api.post("/graphql", json={"query": "{ player(id: 605141) { fullName } }"})
    assert response.json() == {"data": {"player": {"fullName": None}}}


def test_roster(api, mlb, roster_player):
    mlb.get_team_roster.return_value = [roster_player]
    response = api.post("/graphql", json={"query": ROSTER_QUERY})
    assert response.json() == {"data": {"team": {
        "id": 136, "name": "Seattle Mariners", "roster": [{
            "player": {"id": 605141, "fullName": "Mookie Betts"},
            "position": {"abbreviation": "SS"},
        }],
    }}}
    mlb.get_team.assert_awaited_once_with(136)
    mlb.get_team_roster.assert_awaited_once_with(136, rosterType="active")
    mlb.get_person.assert_not_awaited()


def test_empty_roster(api, mlb):
    mlb.get_team_roster.return_value = []
    response = api.post("/graphql", json={"query": ROSTER_QUERY})
    assert response.json()["data"]["team"]["roster"] == []
    assert "errors" not in response.json()


def test_optional_roster_fields(api, mlb, roster_player):
    roster_player.full_name = None
    roster_player.primary_position = None
    mlb.get_team_roster.return_value = [roster_player]
    response = api.post("/graphql", json={"query": ROSTER_QUERY})
    assert response.json()["data"]["team"]["roster"] == [{
        "player": {"id": 605141, "fullName": None}, "position": None,
    }]
    assert "errors" not in response.json()


def test_nested_field_selection(api, mlb, roster_player):
    mlb.get_team_roster.return_value = [roster_player]
    response = api.post("/graphql", json={"query": """{
        team(id: 136) { roster { player { fullName } } }
    }"""})
    assert response.json() == {"data": {"team": {"roster": [
        {"player": {"fullName": "Mookie Betts"}}
    ]}}}
    mlb.get_team_roster.assert_awaited_once_with(136, rosterType="active")
    mlb.get_person.assert_not_awaited()


@pytest.mark.parametrize("query", [
    "{ team(id: 136) { id name } }",
    "{ team(id: 136) { id roster @skip(if: true) { player { id } } } }",
])
def test_roster_is_lazy(api, mlb, query):
    response = api.post("/graphql", json={"query": query})
    assert "errors" not in response.json()
    mlb.get_team.assert_awaited_once_with(136)
    mlb.get_team_roster.assert_not_awaited()
    mlb.get_person.assert_not_awaited()


def test_missing_team_does_not_fetch_roster(api, mlb):
    mlb.get_team.return_value = None
    response = api.post("/graphql", json={"query": ROSTER_QUERY})
    assert response.json() == {"data": {"team": None}}
    mlb.get_team_roster.assert_not_awaited()


@pytest.mark.parametrize("field", ["player", "roster"])
@pytest.mark.parametrize("error", [
    MlbTimeoutError("private timeout"), MlbTransportError("private transport"),
    MlbHttpError(503, "private HTTP"), MlbDecodeError("private JSON"),
    ValidationError.from_exception_data("Person", [
        {"type": "missing", "loc": ("id",), "input": {"private": "payload"}},
    ]),
])
def test_new_upstream_errors(api, mlb, field, error):
    if field == "player":
        mlb.get_person.side_effect = error
        query = "{ player(id: 605141) { id } team(id: 136) { name } }"
        data = {"player": None, "team": {"name": "Seattle Mariners"}}
        path = ["player"]
    else:
        mlb.get_team_roster.side_effect = error
        query = ROSTER_QUERY
        data = {"team": {"id": 136, "name": "Seattle Mariners", "roster": None}}
        path = ["team", "roster"]
    response = api.post("/graphql", json={"query": query})
    assert response.status_code == 200
    body = response.json()
    assert body["data"] == data
    assert len(body["errors"]) == 1
    assert body["errors"][0]["extensions"] == {"code": "UPSTREAM_ERROR"}
    assert body["errors"][0]["path"] == path
    assert body["errors"][0]["message"] == (
        "The MLB service is currently unavailable. Please try again later."
    )
    assert "private" not in response.text


# M1.1: input validation, error sanitization, and module structure.

@pytest.mark.parametrize("field", ["team", "player"])
@pytest.mark.parametrize("id", [0, -1, -136])
def test_non_positive_id_is_rejected_without_upstream_call(api, mlb, field, id):
    response = api.post("/graphql", json={"query": f"{{ {field}(id: {id}) {{ id }} }}"})
    assert response.status_code == 200
    body = response.json()
    assert body["data"] == {field: None}
    assert body["errors"] == [{
        "message": "Argument 'id' must be a positive integer.",
        "locations": [{"line": 1, "column": 3}],
        "path": [field],
        "extensions": {"code": "BAD_USER_INPUT"},
    }]
    mlb.get_team.assert_not_awaited()
    mlb.get_person.assert_not_awaited()
    mlb.get_team_roster.assert_not_awaited()


def test_non_positive_id_via_variable(api, mlb):
    response = api.post("/graphql", json={
        "query": "query($id: Int!) { team(id: $id) { roster { player { id } } } }",
        "variables": {"id": -1},
    })
    body = response.json()
    assert body["data"] == {"team": None}
    assert [e["extensions"] for e in body["errors"]] == [{"code": "BAD_USER_INPUT"}]
    mlb.get_team.assert_not_awaited()
    mlb.get_team_roster.assert_not_awaited()


def test_invalid_and_valid_ids_in_one_query(api, mlb):
    response = api.post("/graphql", json={
        "query": "{ bad: team(id: 0) { id } good: team(id: 136) { id } }"
    })
    body = response.json()
    assert body["data"] == {"bad": None, "good": {"id": 136}}
    assert [e["extensions"]["code"] for e in body["errors"]] == ["BAD_USER_INPUT"]
    mlb.get_team.assert_awaited_once_with(136)


def test_minimum_positive_id_is_forwarded(api, mlb):
    mlb.get_person.return_value = None
    response = api.post("/graphql", json={"query": "{ player(id: 1) { id } }"})
    assert response.json() == {"data": {"player": None}}
    mlb.get_person.assert_awaited_once_with(1)


@pytest.mark.parametrize("query, method", [
    ("{ team(id: 136) { id } }", "get_team"),
    ("{ player(id: 605141) { id } }", "get_person"),
    (ROSTER_QUERY, "get_team_roster"),
])
def test_unexpected_exception_is_sanitized(api, mlb, caplog, query, method):
    getattr(mlb, method).side_effect = RuntimeError("secret internal detail")
    response = api.post("/graphql", json={"query": query})
    assert response.status_code == 200
    body = response.json()
    assert len(body["errors"]) == 1
    assert body["errors"][0]["message"] == "Unexpected error."
    assert "extensions" not in body["errors"][0]
    assert "secret" not in response.text
    assert "RuntimeError" not in response.text
    # The original exception and traceback are still logged server-side.
    assert any(
        "secret internal detail" in str(record.exc_info[1])
        for record in caplog.records if record.exc_info
    )


def test_upstream_error_is_not_masked(api, mlb):
    mlb.get_person.side_effect = MlbTimeoutError("private")
    response = api.post("/graphql", json={"query": "{ player(id: 605141) { id } }"})
    assert response.json()["errors"][0]["extensions"] == {"code": "UPSTREAM_ERROR"}


def test_validation_errors_are_not_masked(api):
    response = api.post("/graphql", json={"query": "{ team(id: 136) { unknownField } }"})
    assert "unknownField" in response.json()["errors"][0]["message"]


def test_public_schema_preserves_existing_fields_and_adds_season_stats():
    from mlb_statsapi_graphql.schema import schema

    assert schema.as_str() == """\
type Player {
  id: Int!
  fullName: String
}

type Position {
  abbreviation: String
}

type Query {
  player(id: Int!): Player
  team(id: Int!): Team
}

type RosterEntry {
  player: Player!
  position: Position
}

type Team {
  id: Int!
  name: String
  abbreviation: String
  stats(season: Int!): TeamSeasonStats
  roster: [RosterEntry!]
}

type TeamSeasonStats {
  season: Int!
  wins: Int
  losses: Int
  runsScored: Int
  runsAllowed: Int
}"""


def test_types_module_does_not_import_resolvers():
    import ast
    from pathlib import Path

    import mlb_statsapi_graphql.types as types_module

    tree = ast.parse(Path(types_module.__file__).read_text())
    imported = {
        node.module for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert not imported & {"resolvers", "schema", "app"}


# M2.1: request-scoped DataLoaders.

UPSTREAM_METHODS = ("get_team", "get_person", "get_team_roster")


def upstream_calls(mlb) -> dict[str, int]:
    """Count awaited calls per mocked AsyncMlb method, e.g. {"get_team": 1, ...}."""
    return {method: getattr(mlb, method).await_count for method in UPSTREAM_METHODS}


def team_model(id: int) -> Team:
    return Team(id=id, link=f"/api/v1/teams/{id}", name=f"Team {id}", abbreviation=f"T{id}")


ALIAS_ROSTER_QUERY = """{
    first: team(id: 136) { name roster { player { fullName } } }
    second: team(id: 136) { name roster { player { fullName } } }
}"""


@pytest.mark.parametrize("query, expected", [
    # Counts before M2.1 (no DataLoaders) were 2/0/2, 0/2/0, 3/0/0, 2/0/0, 1/0/1.
    (ALIAS_ROSTER_QUERY, {"get_team": 1, "get_person": 0, "get_team_roster": 1}),
    ("{ a: player(id: 605141) { id } b: player(id: 605141) { fullName } }",
     {"get_team": 0, "get_person": 1, "get_team_roster": 0}),
    ("{ a: team(id: 136) { id } b: team(id: 136) { name } c: team(id: 136) { abbreviation } }",
     {"get_team": 1, "get_person": 0, "get_team_roster": 0}),
    ("{ a: team(id: 136) { id } b: team(id: 147) { id } }",
     {"get_team": 2, "get_person": 0, "get_team_roster": 0}),
    (ROSTER_QUERY, {"get_team": 1, "get_person": 0, "get_team_roster": 1}),
])
def test_upstream_call_counts(api, mlb, roster_player, query, expected):
    mlb.get_team.side_effect = team_model
    mlb.get_person.return_value = Person(id=605141, link="/x", fullName="Mookie Betts")
    mlb.get_team_roster.return_value = [roster_player]
    response = api.post("/graphql", json={"query": query})
    assert "errors" not in response.json()
    assert upstream_calls(mlb) == expected


def test_team_aliases_share_team_and_roster_fetch(api, mlb, roster_player):
    mlb.get_team_roster.return_value = [roster_player]
    response = api.post("/graphql", json={"query": ALIAS_ROSTER_QUERY})
    team = {"name": "Seattle Mariners", "roster": [{"player": {"fullName": "Mookie Betts"}}]}
    assert response.json() == {"data": {"first": team, "second": team}}
    mlb.get_team.assert_awaited_once_with(136)
    mlb.get_team_roster.assert_awaited_once_with(136, rosterType="active")
    mlb.get_person.assert_not_awaited()


def test_player_aliases_share_one_fetch(api, mlb):
    mlb.get_person.return_value = Person(id=605141, link="/x", fullName="Mookie Betts")
    response = api.post("/graphql", json={
        "query": "{ a: player(id: 605141) { id } b: player(id: 605141) { id fullName } }"
    })
    assert response.json() == {"data": {
        "a": {"id": 605141}, "b": {"id": 605141, "fullName": "Mookie Betts"},
    }}
    mlb.get_person.assert_awaited_once_with(605141)


def test_repeated_roster_on_one_team_shares_one_fetch(api, mlb, roster_player):
    mlb.get_team_roster.return_value = [roster_player]
    response = api.post("/graphql", json={"query": """{ team(id: 136) {
        a: roster { player { id } }
        b: roster { position { abbreviation } }
    } }"""})
    assert response.json() == {"data": {"team": {
        "a": [{"player": {"id": 605141}}], "b": [{"position": {"abbreviation": "SS"}}],
    }}}
    mlb.get_team_roster.assert_awaited_once_with(136, rosterType="active")


def test_separate_http_requests_do_not_share_cache(api, mlb):
    for _ in range(2):
        response = api.post("/graphql", json={"query": "{ team(id: 136) { id } }"})
        assert response.json() == {"data": {"team": {"id": 136}}}
    assert mlb.get_team.await_count == 2


def test_each_request_gets_new_loaders(mlb):
    from mlb_statsapi_graphql.context import Context

    import asyncio

    semaphore = asyncio.Semaphore(10)
    first, second = Context(mlb, semaphore), Context(mlb, semaphore)
    assert first.loaders is not second.loaders
    assert first.loaders.team is not second.loaders.team


def test_different_ids_are_fetched_independently(api, mlb):
    mlb.get_team.side_effect = team_model
    response = api.post("/graphql", json={
        "query": "{ a: team(id: 136) { id name } b: team(id: 147) { id name } }"
    })
    assert response.json() == {"data": {
        "a": {"id": 136, "name": "Team 136"}, "b": {"id": 147, "name": "Team 147"},
    }}
    assert sorted(c.args for c in mlb.get_team.await_args_list) == [(136,), (147,)]


def test_skipped_fields_do_not_fetch(api, mlb):
    response = api.post("/graphql", json={"query": """{
        a: team(id: 147) @skip(if: true) { id }
        b: player(id: 605141) @include(if: false) { id }
        c: team(id: 136) { id roster @skip(if: true) { player { id } } }
    }"""})
    assert response.json() == {"data": {"c": {"id": 136}}}
    assert upstream_calls(mlb) == {"get_team": 1, "get_person": 0, "get_team_roster": 0}
    mlb.get_team.assert_awaited_once_with(136)


def test_missing_entities_are_null_for_every_alias(api, mlb):
    mlb.get_team.return_value = None
    mlb.get_person.return_value = None
    response = api.post("/graphql", json={"query": """{
        a: team(id: 999999) { roster { player { id } } } b: team(id: 999999) { id }
        c: player(id: 999999) { id } d: player(id: 999999) { id }
    }"""})
    assert response.json() == {"data": {"a": None, "b": None, "c": None, "d": None}}
    assert upstream_calls(mlb) == {"get_team": 1, "get_person": 1, "get_team_roster": 0}


def test_empty_roster_is_shared_by_aliases(api, mlb):
    mlb.get_team_roster.return_value = []
    response = api.post("/graphql", json={"query": ALIAS_ROSTER_QUERY})
    team = {"name": "Seattle Mariners", "roster": []}
    assert response.json() == {"data": {"first": team, "second": team}}
    mlb.get_team_roster.assert_awaited_once()


@pytest.mark.parametrize("method, query, data, paths", [
    ("get_team", "{ a: team(id: 136) { id } b: team(id: 136) { id } }",
     {"a": None, "b": None}, [["a"], ["b"]]),
    ("get_person", "{ a: player(id: 605141) { id } b: player(id: 605141) { id } }",
     {"a": None, "b": None}, [["a"], ["b"]]),
    ("get_team_roster", ALIAS_ROSTER_QUERY,
     {"first": {"name": "Seattle Mariners", "roster": None},
      "second": {"name": "Seattle Mariners", "roster": None}},
     [["first", "roster"], ["second", "roster"]]),
])
def test_upstream_error_is_reported_per_alias_not_cached_as_empty(
    api, mlb, method, query, data, paths
):
    getattr(mlb, method).side_effect = MlbTimeoutError("private")
    response = api.post("/graphql", json={"query": query})
    body = response.json()
    assert body["data"] == data
    assert sorted(e["path"] for e in body["errors"]) == paths
    assert {e["extensions"]["code"] for e in body["errors"]} == {"UPSTREAM_ERROR"}
    assert "private" not in response.text
    # The failure is shared within the request, so the upstream is called once.
    assert getattr(mlb, method).await_count == 1


def test_upstream_error_is_not_cached_across_requests(api, mlb):
    mlb.get_team.side_effect = [MlbTimeoutError("private"), team_model(136)]
    first = api.post("/graphql", json={"query": "{ team(id: 136) { id } }"}).json()
    assert first["errors"][0]["extensions"] == {"code": "UPSTREAM_ERROR"}
    second = api.post("/graphql", json={"query": "{ team(id: 136) { id } }"}).json()
    assert second == {"data": {"team": {"id": 136}}}
    assert mlb.get_team.await_count == 2


def test_failure_for_one_id_does_not_affect_another(api, mlb):
    def get_team(id):
        if id == 147:
            raise MlbHttpError(503, "private")
        return team_model(id)

    mlb.get_team.side_effect = get_team
    response = api.post("/graphql", json={
        "query": "{ good: team(id: 136) { id } bad: team(id: 147) { id } }"
    })
    body = response.json()
    assert body["data"] == {"good": {"id": 136}, "bad": None}
    assert [(e["path"], e["extensions"]["code"]) for e in body["errors"]] == [
        (["bad"], "UPSTREAM_ERROR")
    ]


def test_unexpected_exception_is_sanitized_for_every_alias(api, mlb):
    mlb.get_team.side_effect = RuntimeError("secret internal detail")
    response = api.post("/graphql", json={"query": "{ a: team(id: 136) { id } b: team(id: 136) { id } }"})
    body = response.json()
    assert body["data"] == {"a": None, "b": None}
    assert [e["message"] for e in body["errors"]] == ["Unexpected error."] * 2
    assert "secret" not in response.text
    mlb.get_team.assert_awaited_once_with(136)


def test_invalid_id_alias_does_not_block_valid_alias(api, mlb):
    response = api.post("/graphql", json={
        "query": "{ bad: player(id: -5) { id } good: team(id: 136) { id } again: team(id: 136) { id } }"
    })
    body = response.json()
    assert body["data"] == {"bad": None, "good": {"id": 136}, "again": {"id": 136}}
    assert [e["extensions"]["code"] for e in body["errors"]] == ["BAD_USER_INPUT"]
    assert upstream_calls(mlb) == {"get_team": 1, "get_person": 0, "get_team_roster": 0}


def test_upstream_failure_is_logged_once_per_fetch(api, mlb, caplog):
    mlb.get_team.side_effect = MlbTimeoutError("private")
    api.post("/graphql", json={"query": "{ a: team(id: 136) { id } b: team(id: 136) { id } }"})
    warnings = [r for r in caplog.records if r.getMessage() == "MLB team lookup failed for 136"]
    assert len(warnings) == 1
    assert isinstance(warnings[0].exc_info[1], MlbTimeoutError)


# M2.2: query depth, cost, alias, and token limits.

COMPLEXITY_REJECTION = {"data": None, "errors": [{
    "message": "Query cost exceeds the configured limit.",
    "extensions": {"code": "QUERY_COMPLEXITY_LIMIT"},
}]}
NO_UPSTREAM_CALLS = {"get_team": 0, "get_person": 0, "get_team_roster": 0}


def teams(count: int, fields: str = "id", first_id: int = 1, prefix: str = "t") -> str:
    """Selections like `t0: team(id: 1) { id } t1: team(id: 2) { id } ...`."""
    return " ".join(
        f"{prefix}{i}: team(id: {first_id + i}) {{ {fields} }}" for i in range(count)
    )


def cost(query: str, variables: dict | None = None, operation_name: str | None = None):
    from graphql import parse

    from mlb_statsapi_graphql.complexity import estimate_cost
    from mlb_statsapi_graphql.schema import schema

    return estimate_cost(schema._schema, parse(query), operation_name, variables)


@pytest.fixture
def limited_api(mlb):
    """Build a client whose schema uses custom limits."""
    from mlb_statsapi_graphql.schema import create_schema
    from mlb_statsapi_graphql.settings import Limits

    def build(**limits):
        app = create_app(mlb_factory=lambda: mlb, schema=create_schema(Limits(**limits)))
        return TestClient(app)

    return build


@pytest.mark.parametrize("query, variables, expected", [
    # Root lookups and nested rosters.
    ("{ team(id: 136) { id name } }", None, 5),
    ("{ player(id: 605141) { id fullName } }", None, 5),
    (ROSTER_QUERY, None, 15),
    ("{ __typename }", None, 0),
    # Aliases: the same ID shares one DataLoader call, so it is charged once.
    ("{ a: team(id: 136) { id } b: team(id: 136) { name } }", None, 5),
    (ALIAS_ROSTER_QUERY, None, 15),
    ("{ team(id: 136) { a: roster { player { id } } b: roster { position { abbreviation } } } }",
     None, 15),
    ("{ a: team(id: 136) { id } b: player(id: 136) { id } }", None, 10),
    # Distinct IDs and rosters on different teams are charged separately.
    ("{ a: team(id: 136) { id } b: team(id: 147) { id } }", None, 10),
    ("{ a: team(id: 136) { roster { player { id } } } b: team(id: 147) { roster { player { id } } } }",
     None, 30),
    ("{ a: team(id: 136) { roster { player { id } } } b: team(id: 136) { id } }", None, 15),
    # Fragments count exactly like the fields they contain.
    ("{ ...Teams } fragment Teams on Query { a: team(id: 1) { id } b: team(id: 2) { id } }",
     None, 10),
    ("{ team(id: 136) { ...WithRoster } } fragment WithRoster on Team { roster { player { id } } }",
     None, 15),
    ("{ ... on Query { team(id: 136) { ... on Team { roster { player { id } } } } } }", None, 15),
    ("{ ...A ...A } fragment A on Query { team(id: 136) { roster { player { id } } } }", None, 15),
    # Invalid IDs never reach upstream but are still charged (an upper bound).
    ("{ team(id: 0) { id } }", None, 5),
    # @skip and @include, with literals and variables.
    ("{ team(id: 136) @skip(if: true) { id } }", None, 0),
    ("{ team(id: 136) @include(if: false) { id } }", None, 0),
    ("{ team(id: 136) { id roster @skip(if: true) { player { id } } } }", None, 5),
    ("query($s: Boolean!) { team(id: 136) { roster @skip(if: $s) { player { id } } } }",
     {"s": True}, 5),
    ("query($s: Boolean!) { team(id: 136) { roster @skip(if: $s) { player { id } } } }",
     {"s": False}, 15),
    ("query($i: Boolean = true) { ...F @include(if: $i) } fragment F on Query { team(id: 1) { id } }",
     None, 5),
    ("query($i: Boolean = true) { ...F @include(if: $i) } fragment F on Query { team(id: 1) { id } }",
     {"i": False}, 0),
    # Variable IDs are compared by value.
    ("query($a: Int!, $b: Int!) { x: team(id: $a) { id } y: team(id: $b) { id } }",
     {"a": 136, "b": 136}, 5),
    ("query($a: Int!, $b: Int!) { x: team(id: $a) { id } y: team(id: $b) { id } }",
     {"a": 136, "b": 147}, 10),
    ("query($a: Int! = 136) { x: team(id: $a) { id } y: team(id: 136) { id } }", None, 5),
])
def test_estimated_cost(query, variables, expected):
    assert cost(query, variables) == expected


def test_only_the_selected_operation_is_costed():
    document = f"query Cheap {{ team(id: 136) {{ id }} }} query Expensive {{ {teams(30)} }}"
    assert cost(document, operation_name="Cheap") == 5
    assert cost(document, operation_name="Expensive") == 150


def test_cost_is_not_estimated_for_operations_that_cannot_execute():
    # graphql-core rejects these itself before any resolver runs.
    assert cost("query A { team(id: 1) { id } } query B { team(id: 2) { id } }") is None
    assert cost("query($id: Int!) { team(id: $id) { id } }", {}) is None
    assert cost("{ team(id: 1) { id } }", operation_name="Missing") is None


@pytest.mark.parametrize("query", [
    "{ team(id: 136) { id name abbreviation } }",
    "{ player(id: 605141) { id fullName } }",
    ROSTER_QUERY,
    # Six teams with rosters cost exactly 90, within the default limit of 100.
    "{ " + teams(6, "id roster { player { id fullName } position { abbreviation } }") + " }",
    # Twenty distinct teams cost exactly 100; the limit is inclusive.
    "{ " + teams(20) + " }",
])
def test_normal_queries_are_accepted(api, mlb, roster_player, query):
    mlb.get_team.side_effect = team_model
    mlb.get_person.return_value = Person(id=605141, link="/x", fullName="Mookie Betts")
    mlb.get_team_roster.return_value = [roster_player]
    body = api.post("/graphql", json={"query": query}).json()
    assert "errors" not in body
    assert all(value is not None for value in body["data"].values())


@pytest.mark.parametrize("query", [
    # Many aliases with distinct IDs: 21 teams cost 105.
    "{ " + teams(21) + " }",
    # Seven teams with rosters cost 105.
    "{ " + teams(7, "roster { player { id } }") + " }",
    # Distinct players amplify the same way.
    "{ " + " ".join(f"p{i}: player(id: {i + 1}) {{ id }}" for i in range(21)) + " }",
    # Fragments, inline fragments, and nested spreads cannot hide the cost.
    "{ ...Many } fragment Many on Query { " + teams(21) + " }",
    "{ ... on Query { ... on Query { " + teams(21) + " } } }",
    "{ ...A ...B } fragment A on Query { " + teams(11) + " } fragment B on Query { "
    + teams(10, first_id=100, prefix="u") + " }",
    "{ " + teams(7, "...R") + " } fragment R on Team { roster { player { id } } }",
])
def test_expensive_queries_are_rejected_before_any_upstream_call(api, mlb, query):
    response = api.post("/graphql", json={"query": query})
    assert response.status_code == 200
    assert response.json() == COMPLEXITY_REJECTION
    assert upstream_calls(mlb) == NO_UPSTREAM_CALLS


def test_duplicate_aliases_are_charged_once(api, mlb, roster_player):
    mlb.get_team_roster.return_value = [roster_player]
    query = "{ " + " ".join(
        f"t{i}: team(id: 136) {{ name roster {{ player {{ id }} }} }}" for i in range(25)
    ) + " }"
    body = api.post("/graphql", json={"query": query}).json()
    assert "errors" not in body
    assert len(body["data"]) == 25
    assert upstream_calls(mlb) == {"get_team": 1, "get_person": 0, "get_team_roster": 1}


def test_directive_variables_decide_whether_fields_are_charged(api, mlb):
    mlb.get_team.side_effect = team_model
    query = "query($all: Boolean!) { first: team(id: 1000) { id } ...Many @include(if: $all) }" \
        " fragment Many on Query { " + teams(20) + " }"
    small = api.post("/graphql", json={"query": query, "variables": {"all": False}}).json()
    assert small == {"data": {"first": {"id": 1000}}}
    assert upstream_calls(mlb) == {"get_team": 1, "get_person": 0, "get_team_roster": 0}

    large = api.post("/graphql", json={"query": query, "variables": {"all": True}}).json()
    assert large == COMPLEXITY_REJECTION
    assert mlb.get_team.await_count == 1  # unchanged by the rejected request


def test_variable_ids_are_deduplicated_like_dataloaders(api, mlb):
    query = "query($a: Int!, $b: Int!) { x: team(id: $a) { id } y: team(id: $b) { id } }"
    body = api.post("/graphql", json={"query": query, "variables": {"a": 136, "b": 136}}).json()
    assert body == {"data": {"x": {"id": 136}, "y": {"id": 136}}}
    mlb.get_team.assert_awaited_once_with(136)


def test_unselected_operation_does_not_count(api, mlb):
    document = f"query Cheap {{ team(id: 136) {{ id }} }} query Expensive {{ {teams(21)} }}"
    cheap = api.post("/graphql", json={"query": document, "operationName": "Cheap"}).json()
    assert cheap == {"data": {"team": {"id": 136}}}
    expensive = api.post("/graphql", json={"query": document, "operationName": "Expensive"})
    assert expensive.json() == COMPLEXITY_REJECTION
    mlb.get_team.assert_awaited_once_with(136)


def test_invalid_variables_keep_the_normal_error(api, mlb):
    body = api.post("/graphql", json={
        "query": "query($id: Int!) { team(id: $id) { id } }", "variables": {"id": "x"},
    }).json()
    assert body["data"] is None
    assert "QUERY_COMPLEXITY_LIMIT" not in str(body)
    assert upstream_calls(mlb) == NO_UPSTREAM_CALLS


def test_complexity_error_is_not_masked(api, mlb):
    # A GraphQLError without an original exception keeps its message and code.
    from mlb_statsapi_graphql.complexity import complexity_error
    from mlb_statsapi_graphql.errors import should_mask_error

    assert not should_mask_error(complexity_error())
    body = api.post("/graphql", json={"query": "{ " + teams(21) + " }"}).json()
    assert "Unexpected error." not in str(body)


def test_cost_limit_is_configurable(limited_api, mlb):
    with limited_api(max_cost=14) as client:
        assert client.post("/graphql", json={"query": ROSTER_QUERY}).json() == COMPLEXITY_REJECTION
        assert client.post("/graphql", json={"query": "{ team(id: 136) { id } }"}).json() == {
            "data": {"team": {"id": 136}}
        }
    assert upstream_calls(mlb) == {"get_team": 1, "get_person": 0, "get_team_roster": 0}


DEEP_QUERY = "{ team(id: 136) { a { b { c { d { e { f { g } } } } } } } }"


def test_excessive_depth_is_rejected_before_execution(api, mlb):
    body = api.post("/graphql", json={"query": DEEP_QUERY}).json()
    assert body["data"] is None
    assert "'anonymous' exceeds maximum operation depth of 6" in [e["message"] for e in body["errors"]]
    assert upstream_calls(mlb) == NO_UPSTREAM_CALLS


@pytest.mark.parametrize("query", [
    ROSTER_QUERY,
    "{ team(id: 136) { ...R } } fragment R on Team { roster { ...P } } fragment P on RosterEntry { player { id } }",
    "query Deep { a: team(id: 136) { b: roster { ... on RosterEntry { c: player { id } } } } }",
])
def test_depth_limit_applies_through_fragments_and_aliases(limited_api, mlb, query):
    # The roster query is 3 levels deep: team -> roster -> player -> scalar.
    with limited_api(max_depth=2) as client:
        body = client.post("/graphql", json={"query": query}).json()
    assert body["data"] is None
    assert "exceeds maximum operation depth of 2" in body["errors"][0]["message"]
    assert upstream_calls(mlb) == NO_UPSTREAM_CALLS


def test_roster_query_fits_a_depth_limit_of_three(limited_api, mlb, roster_player):
    mlb.get_team_roster.return_value = [roster_player]
    with limited_api(max_depth=3) as client:
        body = client.post("/graphql", json={"query": ROSTER_QUERY}).json()
    assert "errors" not in body


def test_too_many_aliases_are_rejected(api, mlb):
    query = "{ " + " ".join(f"a{i}: team(id: 136) {{ id }}" for i in range(31)) + " }"
    body = api.post("/graphql", json={"query": query}).json()
    assert body == {"data": None, "errors": [{"message": "31 aliases found. Allowed: 30"}]}
    assert upstream_calls(mlb) == NO_UPSTREAM_CALLS


def test_aliases_in_fragments_are_counted(api, mlb):
    query = "{ ...F } fragment F on Query { " + " ".join(
        f"a{i}: __typename" for i in range(31)
    ) + " }"
    body = api.post("/graphql", json={"query": query}).json()
    assert body["errors"][0]["message"] == "31 aliases found. Allowed: 30"


def test_oversized_document_is_rejected_while_parsing(api, mlb):
    query = "{ team(id: 136) { " + "id " * 1000 + "} }"
    body = api.post("/graphql", json={"query": query}).json()
    assert body["data"] is None
    assert "more than 1000 tokens" in body["errors"][0]["message"]
    assert upstream_calls(mlb) == NO_UPSTREAM_CALLS


@pytest.mark.parametrize("query", [
    # Fragment cycles are rejected by standard validation, never walked forever.
    "{ ...A } fragment A on Query { ...B } fragment B on Query { ...A }",
    # Unknown fragments and fields.
    "{ ...Missing }",
    "{ team(id: 136) { roster { player { team { id } } } } }",
    # Mutations do not exist.
    "mutation { team(id: 136) { id } }",
])
def test_malformed_documents_make_no_upstream_calls(api, mlb, query):
    response = api.post("/graphql", json={"query": query})
    assert response.json()["errors"]
    assert "QUERY_COMPLEXITY_LIMIT" not in response.text
    assert upstream_calls(mlb) == NO_UPSTREAM_CALLS


def test_introspection_is_within_limits(api):
    # The same options GraphiQL uses for its schema explorer.
    from graphql import get_introspection_query

    query = get_introspection_query(
        descriptions=True, specified_by_url=True, directive_is_repeatable=True,
        schema_description=True, input_value_deprecation=True,
    )
    body = api.post("/graphql", json={"query": query}).json()
    assert "errors" not in body
    assert body["data"]["__schema"]["queryType"] == {"name": "Query", "kind": "OBJECT"}


def test_default_limits():
    from mlb_statsapi_graphql.settings import Limits

    assert Limits() == Limits(max_depth=6, max_cost=100, max_aliases=30, max_tokens=1000)
    assert Limits.from_env({}) == Limits()


def test_limits_from_environment():
    from mlb_statsapi_graphql.settings import Limits

    assert Limits.from_env({"MLB_GRAPHQL_MAX_COST": "150", "MLB_GRAPHQL_MAX_DEPTH": "4"}) == Limits(
        max_cost=150, max_depth=4
    )
    for raw in ("abc", "0", "-5", "1.5"):
        with pytest.raises(ValueError, match="MLB_GRAPHQL_MAX_TOKENS"):
            Limits.from_env({"MLB_GRAPHQL_MAX_TOKENS": raw})

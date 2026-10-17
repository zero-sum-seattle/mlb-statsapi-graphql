"""Offline season statistics contracts using locked upstream models."""
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from mlbstatsapi import AsyncMlb
from mlbstatsapi.models.teams import Team
from mlbstatsapi.models.stats.stats import Stat
from mlbstatsapi.models.stats.hitting import HittingSeason
from mlbstatsapi.models.stats.pitching import PitchingSeason

from mlb_statsapi_graphql.app import create_app

FIELDS = "season wins losses runsScored runsAllowed"
QUERY = "{ team(id:136) { stats(season:2024) { " + FIELDS + " } } }"


def split(group, **metadata):
    model = HittingSeason if group == "hitting" else PitchingSeason
    values = {"runs": 676} if group == "hitting" else {"wins": 85, "losses": 77, "runs": 607}
    return model(stat=values, **metadata)


def response_stats(**metadata):
    return {g: {"season": Stat(group=g, type="season", totalSplits=1,
                              splits=[split(g, **metadata)])} for g in ("hitting", "pitching")}


@pytest.fixture
def mlb():
    client = AsyncMock(spec=AsyncMlb)
    client.__aenter__.return_value = client
    client.get_team.side_effect = lambda id: Team(id=id, link="/x")
    client.get_team_stats.return_value = response_stats(season="2024", team={"id":136,"link":"/x"}, gameType="R")
    return client


@pytest.fixture
def api(mlb):
    with TestClient(create_app(lambda: mlb)) as client:
        yield client


def test_season_stats_regression(api, mlb):
    assert api.post("/graphql", json={"query": QUERY}).json() == {"data": {"team": {"stats": {
        "season":2024, "wins":85, "losses":77, "runsScored":676, "runsAllowed":607,
    }}}}
    mlb.get_team_stats.assert_awaited_once_with(
        136, stats=["season"], groups=["hitting", "pitching"], season=2024, gameType="R",
    )


def test_distinct_keys_aliases_and_request_isolation(api, mlb):
    mlb.get_team_stats.side_effect = lambda id, **kw: response_stats(season=str(kw["season"]))
    query = """query($year:Int!) {
      a:team(id:136){ x:stats(season:$year){season} y:stats(season:2023){season} }
      b:team(id:136){ stats(season:$year){wins} }
      c:team(id:147){ stats(season:$year){wins} }
    }"""
    for _ in range(2):
        body = api.post("/graphql", json={"query":query,"variables":{"year":2024}}).json()
        assert "errors" not in body
        assert body["data"]["a"] == {"x":{"season":2024}, "y":{"season":2023}}
    assert mlb.get_team_stats.await_count == 6
    assert {(c.args[0], c.kwargs["season"]) for c in mlb.get_team_stats.await_args_list} == {
        (136,2024), (136,2023), (147,2024),
    }


@pytest.mark.parametrize("data", [{}, {"hitting":{}}, {"pitching":{"season":None}},
                                    {"hitting":{"season":Stat(group="hitting",type="season",totalSplits=0,splits=None)}}])
def test_missing_stats_are_null(api, mlb, data):
    mlb.get_team_stats.return_value = data
    assert api.post("/graphql", json={"query":QUERY}).json() == {"data":{"team":{"stats":None}}}


@pytest.mark.parametrize("metadata", [
    {}, {"season":"2024"}, {"season":"2023"}, {"gameType":"P"},
    {"team":{"id":147,"link":"/x"}}, {"player":{"id":1,"link":"/x"}},
    {"position":{"abbreviation":"P","code":"1","name":"Pitcher","type":"Pitcher"}},
])
def test_optional_metadata_is_checked_when_present(api, mlb, metadata):
    mlb.get_team_stats.return_value = response_stats(**metadata)
    body = api.post("/graphql", json={"query":QUERY}).json()
    assert "errors" not in body
    assert (body["data"]["team"]["stats"] is not None) == (metadata in ({},{"season":"2024"}))


def test_selects_unique_matching_total_instead_of_first_split(api, mlb):
    data = response_stats(season="2024")
    for group in data:
        data[group]["season"].splits.insert(0, split(group, season="2023"))
    mlb.get_team_stats.return_value = data
    test_season_stats_regression(api, mlb)


def test_ambiguous_group_does_not_hide_valid_other_group(api, mlb):
    data = response_stats()
    data["pitching"]["season"].splits.append(split("pitching"))
    mlb.get_team_stats.return_value = data
    body = api.post("/graphql", json={"query":QUERY}).json()
    assert body == {"data":{"team":{"stats":{
        "season":2024,"wins":None,"losses":None,"runsScored":676,"runsAllowed":None,
    }}}}


@pytest.mark.parametrize("group,attribute,value", [
    ("hitting","group","pitching"), ("pitching","type","career"),
])
def test_block_metadata_must_match(api, mlb, group, attribute, value):
    data = response_stats()
    setattr(data[group]["season"], attribute, value)
    mlb.get_team_stats.return_value = data
    body = api.post("/graphql", json={"query":QUERY}).json()["data"]["team"]["stats"]
    assert body["runsScored" if group == "hitting" else "wins"] is None


def test_partial_values_and_zero(api, mlb):
    data = response_stats()
    data["hitting"]["season"].splits[0].stat.runs = 0
    pitching = data["pitching"]["season"].splits[0].stat
    pitching.wins, pitching.losses, pitching.runs = 0, None, 0
    mlb.get_team_stats.return_value = data
    assert api.post("/graphql", json={"query":QUERY}).json() == {"data":{"team":{"stats":{
        "season":2024,"wins":0,"losses":None,"runsScored":0,"runsAllowed":0,
    }}}}


@pytest.mark.parametrize("year", [-1,0,999,10000])
def test_invalid_season(api, mlb, year):
    body = api.post("/graphql", json={"query":QUERY.replace("2024",str(year))}).json()
    assert body["data"] == {"team":{"stats":None}}
    assert body["errors"][0]["extensions"]["code"] == "BAD_USER_INPUT"
    mlb.get_team_stats.assert_not_awaited()


@pytest.mark.parametrize("year", [1000,9999])
def test_structural_bounds_do_not_promise_coverage(api, mlb, year):
    mlb.get_team_stats.return_value = {}
    body = api.post("/graphql", json={"query":QUERY.replace("2024",str(year))}).json()
    assert body == {"data":{"team":{"stats":None}}}
    assert mlb.get_team_stats.await_args.kwargs["season"] == year


def test_lazy_stats(api, mlb):
    api.post("/graphql", json={"query":"{team(id:136){id}}"})
    mlb.get_team_stats.assert_not_awaited()


@pytest.mark.parametrize("unexpected", [False,True])
def test_failure_cached_per_key_and_masked(api, mlb, unexpected):
    from mlbstatsapi.exceptions import MlbTimeoutError
    error = RuntimeError("secret") if unexpected else MlbTimeoutError("secret")
    mlb.get_team_stats.side_effect = error
    query = "{team(id:136){a:stats(season:2024){wins} b:stats(season:2024){wins}}}"
    for _ in range(2):
        response = api.post("/graphql", json={"query":query})
        body = response.json()
        assert body["data"] == {"team":{"a":None,"b":None}}
        assert len(body["errors"]) == 2
        assert "secret" not in response.text
        if unexpected:
            assert {e["message"] for e in body["errors"]} == {"Unexpected error."}
        else:
            assert {e["extensions"]["code"] for e in body["errors"]} == {"UPSTREAM_ERROR"}
    assert mlb.get_team_stats.await_count == 2


@pytest.mark.parametrize("query,variables,expected", [
    (QUERY,None,10),
    ("{a:team(id:136){stats(season:2024){wins}} b:team(id:136){stats(season:2024){losses}}}",None,10),
    ("{a:team(id:136){stats(season:2024){wins}} b:team(id:147){stats(season:2024){wins}}}",None,20),
    ("{team(id:136){a:stats(season:2024){wins} b:stats(season:2023){wins}}}",None,15),
    ("query($s:Int!,$skip:Boolean!){team(id:136){...F @skip(if:$skip)}} fragment F on Team {stats(season:$s){wins}}",{"s":2024,"skip":False},10),
    ("query($s:Int!,$skip:Boolean!){team(id:136){...F @skip(if:$skip)}} fragment F on Team {stats(season:$s){wins}}",{"s":2024,"skip":True},5),
    ("{team(id:136){... on Team {stats(season:2024) @include(if:false){wins}}}}",None,5),
    ("{team(id:136){roster{player{id}} stats(season:2024){wins}}}",None,20),
])
def test_cost(query, variables, expected):
    from graphql import parse
    from mlb_statsapi_graphql.complexity import estimate_cost
    from mlb_statsapi_graphql.schema import schema
    assert estimate_cost(schema._schema,parse(query),None,variables) == expected


def test_cost_rejection_precedes_all_lookups(mlb):
    from mlb_statsapi_graphql.schema import create_schema
    from mlb_statsapi_graphql.settings import Limits
    with TestClient(create_app(lambda:mlb,schema=create_schema(Limits(max_cost=9)))) as api:
        body = api.post("/graphql",json={"query":QUERY}).json()
    assert body["errors"][0]["extensions"]["code"] == "QUERY_COMPLEXITY_LIMIT"
    for method in (mlb.get_team,mlb.get_person,mlb.get_team_roster,mlb.get_team_stats):
        method.assert_not_awaited()


@pytest.mark.parametrize("cancel_queued", [False,True])
def test_stats_share_admission_observations_and_cleanup(mlb, caplog, cancel_queued):
    import asyncio
    import logging
    from mlb_statsapi_graphql.context import Context
    from test_upstream import ObservedSemaphore

    caplog.set_level(logging.INFO, logger="mlb_statsapi_graphql.upstream")

    async def scenario():
        semaphore = ObservedSemaphore(1)
        first, second = Context(mlb,semaphore), Context(mlb,semaphore)
        entered, release = asyncio.Event(), asyncio.Event()

        async def blocked(*args, **kwargs):
            entered.set()
            await release.wait()
            return {}

        mlb.get_team_stats.side_effect = blocked
        active = first.loaders.team_stats.load((136,2024))
        await entered.wait()
        queued = second.loaders.team.load(147)
        await semaphore.waiters.get()
        assert first.lookups.stats.lookup_count == 1
        assert second.lookups.stats.lookup_count == 0
        mlb.get_team.assert_not_awaited()
        if cancel_queued:
            await second.lookups.aclose()
            with pytest.raises(asyncio.CancelledError):
                await queued
            assert second.lookups.stats.cancellation_count == 1
            release.set()
            assert await active == {}
        else:
            await first.lookups.aclose()
            with pytest.raises(asyncio.CancelledError):
                await active
            assert first.lookups.stats.cancellation_count == 1
            assert (await queued).id == 147
        # A second stats key proves the shared permit was returned.
        release.set()
        third = Context(mlb,semaphore)
        assert await third.loaders.team_stats.load((136,2023)) == {}
        await third.lookups.aclose()
        await first.lookups.aclose()
        await second.lookups.aclose()

    asyncio.run(asyncio.wait_for(scenario(),5))
    records = [r for r in caplog.records if getattr(r,"lookup_kind",None) == "team_stats"]
    assert len(records) == 2
    assert all(r.admitted for r in records)
    assert {r.outcome for r in records} == ({"success"} if cancel_queued else {"success","cancelled"})


def test_empty_and_all_null_totals(api, mlb):
    for empty_splits in (False, True):
        data = response_stats()
        for group in data:
            block = data[group]["season"]
            if empty_splits:
                block.splits = []
            else:
                block.splits[0].stat.runs = None
                if group == "pitching":
                    block.splits[0].stat.wins = block.splits[0].stat.losses = None
        mlb.get_team_stats.return_value = data
        assert api.post("/graphql", json={"query":QUERY}).json() == {"data":{"team":{"stats":None}}}


def test_validation_failure_is_upstream_error_and_other_key_succeeds(api, mlb):
    from pydantic import ValidationError
    with pytest.raises(ValidationError) as invalid:
        PitchingSeason(stat={"wins":"invalid"})

    def get_stats(id, **kwargs):
        if kwargs["season"] == 2023:
            raise invalid.value
        return response_stats()

    mlb.get_team_stats.side_effect = get_stats
    query = "{team(id:136){good:stats(season:2024){wins} bad:stats(season:2023){wins}}}"
    body = api.post("/graphql", json={"query":query}).json()
    assert body["data"] == {"team":{"good":{"wins":85},"bad":None}}
    assert body["errors"][0]["extensions"]["code"] == "UPSTREAM_ERROR"
    assert mlb.get_team_stats.await_count == 2


def test_stats_request_observations_include_cached_failure_once(api, mlb, caplog):
    import logging
    from mlbstatsapi.exceptions import MlbTimeoutError
    from test_upstream import summaries

    caplog.set_level(logging.INFO, logger="mlb_statsapi_graphql.upstream")
    mlb.get_team_stats.side_effect = MlbTimeoutError("private")
    query = "{team(id:136){a:stats(season:2024){wins} b:stats(season:2024){wins}}}"
    for _ in range(2):
        assert api.post("/graphql", json={"query":query}).json()["errors"]
    records = summaries(caplog)
    assert len(records) == 2
    assert len({r.request_id for r in records}) == 2
    assert all(r.lookup_count == 2 and r.error_count == 1 for r in records)
    lookups = [r for r in caplog.records if getattr(r,"lookup_kind",None) == "team_stats"]
    assert len(lookups) == 2
    assert all(r.outcome == "error" for r in lookups)


def test_combined_fragments_defaults_and_parent_ids():
    from graphql import parse
    from mlb_statsapi_graphql.complexity import estimate_cost
    from mlb_statsapi_graphql.schema import schema

    query = """query($id:Int! = 136, $year:Int! = 2024, $include:Boolean! = true) {
      ...Teams
      duplicate:team(id:$id){ stats(season:$year){wins} }
    }
    fragment Teams on Query {
      first:team(id:$id){ ...Stats }
      second:team(id:147){ ...Stats }
    }
    fragment Stats on Team {
      ... on Team {
        stats(season:$year) @include(if:$include){wins}
        old:stats(season:2023){wins}
      }
    }"""
    assert estimate_cost(schema._schema, parse(query), None, {}) == 30
    assert estimate_cost(schema._schema, parse(query), None, {"include":False}) == 25


def test_stats_cancelled_while_queued_never_call_library(mlb):
    import asyncio
    from mlb_statsapi_graphql.context import Context
    from test_upstream import ObservedSemaphore

    async def scenario():
        semaphore = ObservedSemaphore(1)
        await semaphore.acquire()
        context = Context(mlb, semaphore)
        pending = context.loaders.team_stats.load((136, 2024))
        await semaphore.waiters.get()
        await context.lookups.aclose()
        with pytest.raises(asyncio.CancelledError):
            await pending
        mlb.get_team_stats.assert_not_awaited()
        assert context.lookups.stats.lookup_count == 0
        assert context.lookups.stats.cancellation_count == 1
        semaphore.release()

    asyncio.run(asyncio.wait_for(scenario(), 5))

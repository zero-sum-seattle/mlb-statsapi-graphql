"""Offline admission tests use events, never elapsed-time scheduling guesses."""

import asyncio
import logging
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient
from mlbstatsapi import AsyncMlb
from mlbstatsapi.exceptions import MlbTimeoutError
from mlbstatsapi.models.teams import Team

from mlb_statsapi_graphql.app import create_app
from mlb_statsapi_graphql.schema import create_schema
from mlb_statsapi_graphql.settings import Limits, UpstreamSettings
from mlb_statsapi_graphql.upstream import RequestLookups


class ObservedSemaphore(asyncio.Semaphore):
    """Signal that a caller reached admission while all permits were held."""
    def __init__(self, value):
        super().__init__(value)
        self.waiters = asyncio.Queue()

    async def acquire(self):
        if self.locked():
            self.waiters.put_nowait(True)
        return await super().acquire()


def fake_mlb():
    mlb = AsyncMock(spec=AsyncMlb)
    mlb.__aenter__.return_value = mlb
    return mlb


def summaries(caplog):
    return [r for r in caplog.records if getattr(r, "event", None) == "mlb_graphql_request"]


def test_shared_cap_across_overlapping_http_requests_and_types(caplog):
    caplog.set_level(logging.INFO, logger="mlb_statsapi_graphql.upstream")

    async def scenario():
        mlb = fake_mlb()
        app = create_app(lambda: mlb, upstream_settings=UpstreamSettings(2))
        roster_started, person_started, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        active = peak = 0
        calls = []

        async def lookup(kind, id):
            nonlocal active, peak
            calls.append((kind, id))
            active += 1
            peak = max(peak, active)
            try:
                if kind == "roster":
                    roster_started.set()
                if kind == "person":
                    person_started.set()
                # The first team completes so its lazy roster can occupy a permit.
                if (kind, id) != ("team", 136):
                    await release.wait()
                if kind == "team":
                    return Team(id=id, link=f"/teams/{id}")
                return [] if kind == "roster" else None
            finally:
                active -= 1

        async def team(id): return await lookup("team", id)
        async def person(id): return await lookup("person", id)
        async def roster(id, **kw): return await lookup("roster", id)
        mlb.get_team.side_effect = team
        mlb.get_person.side_effect = person
        mlb.get_team_roster.side_effect = roster

        async with app.router.lifespan_context(app):
            # Prove the lifespan-created semaphore has exactly two permits.
            actual = app.state.upstream_semaphore
            await actual.acquire()
            assert not actual.locked()
            await actual.acquire()
            assert actual.locked()
            actual.release()
            actual.release()
            # Observe admission without inspecting private waiters or depending
            # on task order.
            semaphore = ObservedSemaphore(2)
            app.state.upstream_semaphore = semaphore
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as api:
                async def post(query):
                    return await api.post("/graphql", json={"query": query})
                first = asyncio.create_task(post("{ team(id:136) { roster { player { id } } } }"))
                await roster_started.wait()
                second = asyncio.create_task(post("{ player(id:1) { id } }"))
                await person_started.wait()
                third = asyncio.create_task(post("{ a:team(id:137){id} b:team(id:137){id} c:player(id:2){id} d:player(id:2){id} }"))
                await semaphore.waiters.get()
                await semaphore.waiters.get()
                assert calls == [("team", 136), ("roster", 136), ("person", 1)]
                release.set()
                responses = await asyncio.gather(first, second, third)
                assert all("errors" not in response.json() for response in responses)
        assert peak == 2
        assert calls.count(("team", 137)) == calls.count(("person", 2)) == 1
        assert active == 0
        mlb.__aexit__.assert_awaited_once()

    asyncio.run(asyncio.wait_for(scenario(), 5))
    records = summaries(caplog)
    assert sorted(r.lookup_count for r in records) == [1, 2, 2]
    assert len({r.request_id for r in records}) == 3
    assert all(r.error_count == r.cancellation_count == 0 for r in records)
    lookups = [r for r in caplog.records if getattr(r, "event", None) == "mlb_upstream_lookup"]
    assert len(lookups) == 5
    assert {r.lookup_kind for r in lookups} == {"team", "person", "roster"}
    for record in records:
        assert record.lookup_count == sum(r.request_id == record.request_id for r in lookups)


@pytest.mark.parametrize("mode", ["error", "cancel_active", "cancel_queued"])
def test_permits_are_released_and_counts_start_only_after_admission(mode):
    async def scenario():
        semaphore = ObservedSemaphore(1)
        lookups = RequestLookups(semaphore)
        started, release = asyncio.Event(), asyncio.Event()

        async def blocked():
            started.set()
            await release.wait()
            if mode == "error":
                raise MlbTimeoutError("private detail")

        active = asyncio.create_task(lookups.run("team", blocked))
        await started.wait()
        other = RequestLookups(semaphore)
        fetch = AsyncMock(return_value="next")
        queued = asyncio.create_task(other.run("person", fetch))
        await semaphore.waiters.get()
        assert lookups.stats.lookup_count == 1
        assert other.stats.lookup_count == 0
        if mode == "cancel_queued":
            queued.cancel()
            with pytest.raises(asyncio.CancelledError):
                await queued
            assert other.stats.lookup_count == 0
            assert other.stats.cancellation_count == 1
            fetch.assert_not_awaited()
            release.set()
            await active
        elif mode == "cancel_active":
            active.cancel()
            with pytest.raises(asyncio.CancelledError):
                await active
            assert lookups.stats.cancellation_count == 1
        else:
            release.set()
            with pytest.raises(MlbTimeoutError):
                await active
            assert lookups.stats.error_count == 1
        if mode != "cancel_queued":
            assert await queued == "next"
            assert other.stats.lookup_count == 1
        # A subsequent lookup proves capacity is reusable after every exit path.
        assert await other.run("roster", AsyncMock(return_value="released")) == "released"

    asyncio.run(asyncio.wait_for(scenario(), 5))


def test_queue_wait_and_library_duration_use_separate_clock_intervals(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="mlb_statsapi_graphql.upstream")
    clock = iter([10.0, 13.0, 20.0])
    monkeypatch.setattr("mlb_statsapi_graphql.upstream.monotonic", lambda: next(clock))

    async def scenario():
        lookups = RequestLookups(asyncio.Semaphore(1))
        assert await lookups.run("team", AsyncMock(return_value=None)) is None
        assert lookups.stats.lookup_count == 1
        assert lookups.stats.queue_wait_seconds == 3.0
        assert lookups.stats.lookup_seconds == 7.0

    asyncio.run(scenario())
    record = caplog.records[-1]
    assert (record.queue_wait_seconds, record.lookup_seconds, record.outcome) == (3, 7, "success")


@pytest.mark.parametrize("limits,query,variables", [
    ({}, "{ team(id:1) { unknown } }", None),
    ({}, "{", None),
    ({}, "query($id:Int!){team(id:$id){id}}", {"id": "bad"}),
    ({}, "{ team(id:0){id} }", None),
    ({"max_cost": 1}, "{ team(id:1){id} }", None),
    ({"max_depth": 1}, "{ team(id:1){roster{player{id}}} }", None),
    ({"max_aliases": 1}, "{ a:team(id:1){id} b:team(id:2){id} }", None),
    ({"max_tokens": 2}, "{ team(id:1){id} }", None),
])
def test_rejected_queries_log_zero_lookups(caplog, limits, query, variables):
    caplog.set_level(logging.INFO, logger="mlb_statsapi_graphql.upstream")
    mlb = fake_mlb()
    with TestClient(create_app(lambda: mlb, schema=create_schema(Limits(**limits)))) as api:
        response = api.post("/graphql", json={"query": query, "variables": variables})
    assert response.json()["errors"]
    for method in (mlb.get_team, mlb.get_person, mlb.get_team_roster):
        method.assert_not_awaited()
    record, = summaries(caplog)
    assert record.lookup_count == record.error_count == record.cancellation_count == 0
    assert record.queue_wait_seconds == record.lookup_seconds == 0


def test_concurrency_configuration(monkeypatch):
    assert UpstreamSettings.from_env({}).max_concurrency == 10
    assert UpstreamSettings.from_env({"MLB_GRAPHQL_MAX_CONCURRENCY": "3"}).max_concurrency == 3
    for raw in ("", "no", "1.5", "0", "-1"):
        with pytest.raises(ValueError, match="MLB_GRAPHQL_MAX_CONCURRENCY"):
            UpstreamSettings.from_env({"MLB_GRAPHQL_MAX_CONCURRENCY": raw})
    for value in (0, -1, 1.5, True):
        with pytest.raises(ValueError):
            UpstreamSettings(value)
    monkeypatch.setenv("MLB_GRAPHQL_MAX_CONCURRENCY", "0")
    with pytest.raises(ValueError):
        create_app()


@pytest.mark.parametrize("stats", [False, True])
def test_cancelled_http_request_cleans_up_detached_loader_work(caplog, stats):
    caplog.set_level(logging.INFO, logger="mlb_statsapi_graphql.upstream")

    async def scenario():
        mlb = fake_mlb()
        entered, exited = asyncio.Event(), asyncio.Event()

        async def blocked(id, **kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                exited.set()

        if stats:
            mlb.get_team.return_value = Team(id=136, link="/x")
            mlb.get_team_stats.side_effect = blocked
        else:
            mlb.get_team.side_effect = blocked
        app = create_app(lambda: mlb, upstream_settings=UpstreamSettings(1))
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as api:
                query = (
                    "{team(id:136){stats(season:2024){wins}}}" if stats
                    else "{team(id:136){id}}"
                )
                request = asyncio.create_task(api.post("/graphql", json={"query": query}))
                await entered.wait()
                request.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await request
                assert exited.is_set()
                mlb.get_team.side_effect = None
                mlb.get_team.return_value = None
                response = await api.post("/graphql", json={"query": "{team(id:137){id}}"})
                assert response.json() == {"data": {"team": None}}

    asyncio.run(asyncio.wait_for(scenario(), 5))
    cancelled, successful = summaries(caplog)
    assert cancelled.lookup_count == (2 if stats else 1)
    assert cancelled.cancellation_count == 1
    assert successful.lookup_count == 1
    assert successful.cancellation_count == 0


def test_cached_failure_counts_once_and_next_request_has_fresh_statistics(caplog):
    caplog.set_level(logging.INFO, logger="mlb_statsapi_graphql.upstream")
    mlb = fake_mlb()
    mlb.get_team.side_effect = MlbTimeoutError("private detail")
    query = "{a:team(id:136){id} b:team(id:136){id} player(id:1){id}}"
    mlb.get_person.return_value = None
    with TestClient(create_app(lambda: mlb)) as api:
        for _ in range(2):
            response = api.post("/graphql", json={"query": query})
            body = response.json()
            assert body["data"] == {"a": None, "b": None, "player": None}
            assert len(body["errors"]) == 2
            assert all(e["extensions"]["code"] == "UPSTREAM_ERROR" for e in body["errors"])
            assert "private" not in response.text
    assert mlb.get_team.await_count == mlb.get_person.await_count == 2
    first, second = summaries(caplog)
    assert first.request_id != second.request_id
    for record in (first, second):
        assert record.lookup_count == 2
        assert record.error_count == 1
        assert record.cancellation_count == 0

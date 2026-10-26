"""Offline research probes of the locked dependency; no GraphQL implementation.

Run from the repository root: .venv/bin/python docs/research/m3.2/verify_evidence.py
Optionally pass an upstream checkout to compare the inspected source files.
"""

import asyncio
from copy import deepcopy
from importlib.metadata import version
import json
import logging
import math
from pathlib import Path
import sys
import tomllib

import httpx
import mlbstatsapi
from mlbstatsapi import AsyncMlb
from mlbstatsapi._parsers.stats import parse_split_stats
from mlbstatsapi.exceptions import MlbHttpError
from mlbstatsapi.models.stats.hitting import HittingSeason, SimpleHittingSplit
from mlbstatsapi.models.stats.pitching import PitchingSeason, SimplePitchingSplit
from pydantic import ValidationError

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
# Expected HTTP failures below should not obscure the research result summary.
logging.getLogger("mlbstatsapi.async_mlb").disabled = True
locked = tomllib.loads((ROOT / "uv.lock").read_text())
assert next(p["version"] for p in locked["package"] if p["name"] == "python-mlb-statsapi") == "1.1.2"
assert version("python-mlb-statsapi") == "1.1.2"


def body(name):
    capture = json.loads((HERE / "fixtures" / f"{name}.json").read_text())
    assert capture["status"] == 200
    assert capture["source"].startswith("https://statsapi.mlb.com/api/v1/")
    return capture["body"]


def block(rows, group="hitting", stat_type="season", **metadata):
    return {"type": {"displayName": stat_type}, "group": {"displayName": group},
            "splits": rows, **metadata}


def parse_block(value):
    return parse_split_stats({"stats": [value]})


# Real captures, replayed through the installed parser, not model_construct().
for name in ("soto-2024", "soto-2022", "scherzer-2023", "ohtani-2023", "ohtani-2024", "soto-2022-team"):
    data = parse_split_stats(body(name))
    for group, types in data.items():
        stats = types["season"]
        expected = HittingSeason if group == "hitting" else PitchingSeason
        assert all(isinstance(s, expected) for s in stats.splits)
        assert all(s.season is not None and s.game_type == "R" and s.sport.id == 1 for s in stats.splits)
        print(name, group, [(s.team.id if s.team else None, s.num_teams) for s in stats.splits])

soto = parse_split_stats(body("soto-2022"))["hitting"]["season"]
assert [s.team.id if s.team else None for s in soto.splits] == [None, 120, 135]
assert [s.stat.home_runs for s in soto.splits] == [27, 21, 6]
assert soto.splits[0].num_teams == 2 and soto.total_splits == 3
assert "totalSplits" not in body("soto-2022")["stats"][0]
assert body("soto-2022-team")["stats"] == body("soto-2022")["stats"]
assert set(parse_split_stats(body("ohtani-2023"))) == {"hitting", "pitching"}
assert set(parse_split_stats(body("ohtani-2024"))) == {"hitting"}
scherzer = parse_split_stats(body("scherzer-2023"))["pitching"]["season"].splits
assert [s.stat.innings_pitched for s in scherzer] == ["152.2", "107.2", "45.0"]
assert scherzer[0].num_leagues == 2 and scherzer[0].league is None
assert scherzer[0].stat.outs == 458

# Synthetic counterexamples describe parser behavior, not live API promises.
assert parse_split_stats(None) == parse_split_stats({}) == parse_split_stats({"stats": []}) == {}
assert parse_block(block([])) == {}
rows = [{"season": "2024", "stat": {}}, {"stat": {"runs": 0}}]
parsed = parse_block(block(rows))["hitting"]["season"]
assert parsed.total_splits == 2 and len(parsed.splits) == 1
assert parsed.splits[0].stat.runs == 0 and parsed.splits[0].season is None
assert parse_block(block([{"stat": {"runs": 1}}], stat_type="unknown"))["hitting"]["unknown"].splits == []
assert parse_block(block([{"stat": {"gamesPlayed": 1}}], group="game"))["game"]["season"].splits == []
try:
    parse_block(block([{"stat": {"runs": 1}}], group="unknown"))
except ModuleNotFoundError:
    pass
else:
    raise AssertionError("Unknown group unexpectedly parsed")
first = block([{"stat": {"runs": 1}}])
last = block([{"stat": {"runs": 2}}])
assert parse_split_stats({"stats": [first, last]})["hitting"]["season"].splits[0].stat.runs == 2
fallback = {"splits": [{"type": "gameLog", "group": "hitting", "stat": {"runs": 1}}]}
assert parse_block(fallback)["stats"]["gameLog"].splits[0].stat == {"runs": 1}
unknown = HittingSeason(stat={"futureMetric": 1}, futureScope="unknown", team={})
assert unknown.team is None and "futureScope" not in unknown.model_dump()
assert all(v is None for v in unknown.stat.model_dump().values())
assert SimpleHittingSplit().avg is None
assert SimpleHittingSplit(avg=None, runs=0).runs == 0
assert "avg" not in SimpleHittingSplit().model_fields_set
assert "avg" in SimpleHittingSplit(avg=None).model_fields_set
for sentinel in (".---", "-.--"):
    assert SimpleHittingSplit(avg=sentinel).avg is None
    assert SimplePitchingSplit(era=sentinel).era is None
try:
    SimpleHittingSplit(avg="N/A")
except ValidationError:
    pass
else:
    raise AssertionError("Malformed rate unexpectedly parsed")
assert SimpleHittingSplit(avg=".287").avg == 0.287
assert math.isnan(SimpleHittingSplit(avg="NaN").avg)
assert SimplePitchingSplit(inningsPitched="6.3").innings_pitched == "6.3"
assert SimplePitchingSplit(inningsPitched="6.2").innings_pitched == "6.2"


async def check_methods():
    # The same fixture checks dispatch/arguments for four methods, not the
    # availability of this payload from four different live endpoints.
    calls = [
        ("get_player_stats", (665742,), {"stats": ["season"], "groups": ["hitting", "pitching"]}, "/api/v1/people/665742/stats"),
        ("get_team_stats", (147,), {"stats": ["season"], "groups": ["hitting", "pitching"]}, "/api/v1/teams/147/stats"),
        ("get_stats", (), {"stats": ["season"], "groups": ["hitting", "pitching"]}, "/api/v1/stats"),
        ("get_players_stats_for_game", (665742, 1), {}, "/api/v1/people/665742/stats/game/1"),
    ]
    for method, args, kwargs, path in calls:
        for status in (200, 400, 404, 429, 500):
            def handle(request):
                assert request.url.path == path
                assert request.url.params["season"] == "2024"
                assert request.url.params["gameType"] == "R"
                assert request.url.params["sportIds"] == "1"
                if kwargs:
                    assert request.url.params.get_list("stats") == ["season"]
                    assert request.url.params.get_list("group") == ["hitting", "pitching"]
                return httpx.Response(status, json=deepcopy(body("soto-2024")))

            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
                async with AsyncMlb(client=client) as mlb:
                    try:
                        result = await getattr(mlb, method)(*args, **kwargs, season=2024, gameType="R", sportIds=1)
                    except MlbHttpError as exc:
                        assert status in (400, 429, 500) and exc.status_code == status
                    else:
                        assert status in (200, 404)
                        assert (result == {}) == (status == 404)


asyncio.run(check_methods())
if len(sys.argv) > 1:
    installed = Path(mlbstatsapi.__file__).parent
    checkout = Path(sys.argv[1]) / "mlbstatsapi"
    paths = [Path("async_mlb.py"), Path("mlb_module.py"), Path("_parsers/stats.py"), Path("models/base.py")]
    paths += [p.relative_to(installed) for p in sorted((installed / "models/stats").glob("*.py"))]
    for path in paths:
        assert (installed / path).read_bytes() == (checkout / path).read_bytes(), path
    print(f"{len(paths)} inspected source files match the supplied upstream checkout")
print("Locked-library capture replay, parser counterexamples, and four-method HTTP probes passed")

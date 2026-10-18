"""Live smoke test against the real MLB Stats API.

Starts the GraphQL server with uvicorn (unless --url is given), runs a set of
queries, and checks the responses. Requires outbound HTTPS to statsapi.mlb.com.

    uv run python scripts/live_test.py
    uv run python scripts/live_test.py --url http://127.0.0.1:8000/graphql
"""

import argparse
import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

MARINERS = 136
JULIO_RODRIGUEZ = 677594


def post(url: str, query: str, variables: dict | None = None) -> dict:
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urllib.request.Request(url, body, {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        return json.load(exc)


def check_team(data: dict) -> None:
    team = data["team"]
    assert team["id"] == MARINERS, team
    assert "Mariners" in team["name"], team
    assert team["abbreviation"] == "SEA", team


def check_roster(data: dict) -> None:
    roster = data["team"]["roster"]
    assert roster, "active roster is empty"
    for entry in roster:
        assert entry["player"]["id"] and entry["player"]["fullName"], entry
        assert entry["position"]["abbreviation"], entry


def check_stats(data: dict) -> None:
    stats = data["team"]["stats"]
    assert stats is not None, "stats were null"
    assert stats["season"] == 2024
    # 2024 Mariners finished 85-77.
    assert (stats["wins"], stats["losses"]) == (85, 77), stats
    assert stats["runsScored"] > 0 and stats["runsAllowed"] > 0, stats


def check_player(data: dict) -> None:
    assert data["player"]["fullName"] == "Julio Rodríguez", data


def check_missing(data: dict) -> None:
    assert data["team"] is None, data


def check_aliases(data: dict) -> None:
    assert data["a"] == data["b"], data


CASES = [
    ("team lookup", f"{{ team(id: {MARINERS}) {{ id name abbreviation }} }}", check_team),
    (
        "active roster",
        f"{{ team(id: {MARINERS}) {{ roster {{ player {{ id fullName }} position {{ abbreviation }} }} }} }}",
        check_roster,
    ),
    (
        "season stats",
        f"{{ team(id: {MARINERS}) {{ stats(season: 2024) {{ season wins losses runsScored runsAllowed }} }} }}",
        check_stats,
    ),
    ("player lookup", f"{{ player(id: {JULIO_RODRIGUEZ}) {{ id fullName }} }}", check_player),
    ("missing team", "{ team(id: 999999) { id } }", check_missing),
    (
        "deduplicated aliases",
        f"{{ a: team(id: {MARINERS}) {{ name }} b: team(id: {MARINERS}) {{ name }} }}",
        check_aliases,
    ),
]


def run_cases(url: str) -> int:
    failures = 0
    for name, query, check in CASES:
        start = time.perf_counter()
        result = post(url, query)
        elapsed = (time.perf_counter() - start) * 1000
        try:
            assert not result.get("errors"), result["errors"]
            check(result["data"])
        except (AssertionError, KeyError, TypeError) as exc:
            failures += 1
            print(f"FAIL  {name} ({elapsed:.0f} ms): {exc!r}")
        else:
            print(f"PASS  {name} ({elapsed:.0f} ms)")

    # Complexity limits should reject an oversized query before upstream work.
    big = "{ " + " ".join(f"t{i}: team(id: {i + 100}) {{ roster {{ player {{ id }} }} }}" for i in range(12)) + " }"
    result = post(url, big)
    if result.get("errors"):
        print("PASS  complexity limit rejects expensive query")
    else:
        failures += 1
        print("FAIL  complexity limit did not reject expensive query")
    return failures


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for(url: str, proc: subprocess.Popen, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("server exited during startup")
        try:
            post(url, "{ __typename }")
            return
        except OSError:
            time.sleep(0.25)
    raise RuntimeError("server did not start in time")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", help="use an already running server instead of starting one")
    args = parser.parse_args()

    if args.url:
        failures = run_cases(args.url)
    else:
        port = free_port()
        url = f"http://127.0.0.1:{port}/graphql"
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "mlb_statsapi_graphql.app:app", "--port", str(port)],
        )
        try:
            wait_for(url, proc)
            failures = run_cases(url)
        finally:
            proc.terminate()
            proc.wait(timeout=10)

    print(f"\n{len(CASES) + 1 - failures}/{len(CASES) + 1} live checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

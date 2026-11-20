"""Concurrent-caller load test against a RUNNING backend. Measures the API itself, so start it with the rules planner and
mock speech (LLM_PROVIDER=rules, STT_PROVIDER=mock, TTS_PROVIDER=mock) and a throwaway database: every caller opens a
session, verifies by OTP, then asks balance / statement / loan / card questions.

    backend\\.venv\\Scripts\\python.exe scripts\\load_test.py --callers 50 --turns 6 [--base http://127.0.0.1:8000]

These scripts read the login code from the API response, which only a server started with APP_ENV=test AUTH_MODE=otp OTP_PROVIDER=dev_screen ECHO_DEV_OTP=true STORAGE_BACKEND=memory returns.
The server needs ECHO_DEV_OTP=true and raised OTP_REQUESTS_PER_PHONE / OTP_REQUESTS_PER_IP (all callers share one address).
Exits 1 if any request failed unexpectedly or p95 exceeds --p95-budget-ms.
"""
import argparse
import asyncio
import statistics
import sys
import time

import httpx

sys.stdout.reconfigure(encoding="utf-8")
ap = argparse.ArgumentParser()
ap.add_argument("--base", default="http://127.0.0.1:8000")
ap.add_argument("--callers", type=int, default=50)
ap.add_argument("--turns", type=int, default=6)
ap.add_argument("--p95-budget-ms", type=float, default=1000)
ap.add_argument("--phones", default="9900000101,9900000102,9900000103,9900000104")
args = ap.parse_args()
V1 = args.base.rstrip("/") + "/api/v1"
PHONES = args.phones.split(",")
QUESTIONS = ["What is my balance?", "Show my mini statement", "When is my next EMI due?", "Is my card active?",
             "please increase my credit limit", "hello"]
lat: dict[str, list[float]] = {}
failures: list[str] = []


async def timed(client, name, method, url, **kw):
    t = time.perf_counter()
    try:
        r = await client.request(method, url, **kw)
    except httpx.HTTPError as e:
        failures.append(f"{name}: {type(e).__name__}")
        return None
    lat.setdefault(name, []).append((time.perf_counter() - t) * 1000)
    if r.status_code >= 400:
        failures.append(f"{name}: HTTP {r.status_code}")
    return r


async def caller(i: int, client: httpx.AsyncClient):
    phone = "+91" + PHONES[i % len(PHONES)]
    r = await timed(client, "create_session", "POST", f"{V1}/sessions", json={"consent_microphone": True, "consent_notice": True})
    if r is None or r.status_code != 201:
        return
    h = {"Authorization": f"Bearer {r.json()['session_token']}"}
    r = await timed(client, "otp_request", "POST", f"{V1}/auth/challenges", json={"phone": phone}, headers=h)
    if r is None or r.status_code != 200:
        return
    ch = r.json()
    await timed(client, "otp_verify", "POST", f"{V1}/auth/challenges/{ch['challenge_id']}/verify", json={"code": ch["dev_code"]}, headers=h)
    for t in range(args.turns):
        await timed(client, "agent_turn", "POST", f"{V1}/conversation/turn", json={"text": QUESTIONS[t % len(QUESTIONS)]}, headers=h)
    await timed(client, "get_accounts", "GET", f"{V1}/accounts", headers=h)


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * p))]


async def main() -> int:
    limits = httpx.Limits(max_connections=args.callers * 2)
    async with httpx.AsyncClient(timeout=60, limits=limits) as client:
        # Open every client connection first: on Windows a burst of simultaneous connects adds ~2 s of client-side
        # delay that has nothing to do with the server and would hide its real latency.
        await asyncio.gather(*(client.get(args.base.rstrip('/') + '/healthz') for _ in range(args.callers)))
        t0 = time.perf_counter()
        await asyncio.gather(*(caller(i, client) for i in range(args.callers)))
        wall = time.perf_counter() - t0
    total = sum(len(v) for v in lat.values())
    print(f"{args.callers} concurrent callers, {total} requests in {wall:.1f}s ({total / wall:.0f} req/s)\n")
    print(f"{'endpoint':<16}{'n':>6}{'p50 ms':>9}{'p95 ms':>9}{'p99 ms':>9}{'max ms':>9}")
    for name, xs in lat.items():
        print(f"{name:<16}{len(xs):>6}{statistics.median(xs):>9.0f}{pct(xs, .95):>9.0f}{pct(xs, .99):>9.0f}{max(xs):>9.0f}")
    turn_p95 = pct(lat.get("agent_turn", [0]), .95)
    print(f"\nfailures: {len(failures)}" + ("".join(f"\n  {k}" for k in sorted(set(failures))[:10]) if failures else ""))
    bad = bool(failures) or turn_p95 > args.p95_budget_ms
    print(f"agent_turn p95 {turn_p95:.0f} ms vs budget {args.p95_budget_ms:.0f} ms -> {'FAIL' if bad else 'PASS'}")
    return 1 if bad else 0


sys.exit(asyncio.run(main()))

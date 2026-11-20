"""Measure turn latency with the providers configured in .env and print a report (real measurements only).

    backend\\.venv\\Scripts\\python.exe scripts\\benchmark_voice.py --runs 10

Each run: new session -> OTP -> two agent turns (find + prepare, then confirm). Agent latency (LLM + tools,
local) is reported separately from TTS latency (remote when TTS_PROVIDER=deepgram). Raw audio is not used,
so ASR is NOT measured here; use live_speech_check.py for a single STT sample.
"""
import argparse
import asyncio
import platform
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import httpx  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.infrastructure.metrics import percentile  # noqa: E402
from app.main import create_app  # noqa: E402

TURN_1 = "I sent four thousand two hundred and fifty rupees yesterday but the recipient has not received it"
TURN_2 = "Yes, please register the dispute"


def row(name: str, vals: list[float]) -> str:
    if not vals:
        return f"| {name} | n/a | n/a | n/a | n/a | 0 |"
    return (f"| {name} | {statistics.median(vals):.0f} | {percentile(vals, 90):.0f} | "
            f"{percentile(vals, 95):.0f} | {max(vals):.0f} | {len(vals)} |")


async def main(runs: int) -> None:
    s = get_settings().model_copy(update={"storage_backend": "memory"})  # never touch a real database
    app = create_app(s)
    agent, tts, failures = [], [], 0
    async with app.router.lifespan_context(app):
        ctx = app.state.ctx
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://b", timeout=120) as c:
            for _ in range(runs):
                r = (await c.post("/api/v1/sessions", json={})).json()
                h = {"Authorization": f"Bearer {r['session_token']}"}
                ch = (await c.post("/api/v1/auth/challenges", json={"phone": "+919900000101"}, headers=h)).json()
                await c.post(f"/api/v1/auth/challenges/{ch['challenge_id']}/verify", json={"code": ch["dev_code"]}, headers=h)
                for text in (TURN_1, TURN_2):
                    t0 = time.perf_counter()
                    resp = await c.post("/api/v1/conversation/turn", json={"text": text}, headers=h)
                    agent.append((time.perf_counter() - t0) * 1000)
                    if resp.status_code != 200:
                        failures += 1
                        continue
                    t1 = time.perf_counter()
                    try:
                        await ctx.tts.synthesize(resp.json()["reply"])
                        tts.append((time.perf_counter() - t1) * 1000)
                    except Exception:
                        failures += 1
    print(f"""# Benchmark report
- Date: {time.strftime('%Y-%m-%d %H:%M:%S')}
- OS: {platform.platform()}   Python: {platform.python_version()}   CPU: {platform.processor()}
- LLM: {ctx.llm.name} / {ctx.llm.model}   STT: {ctx.stt.name} (not measured)   TTS: {ctx.tts.name}
- Runs: {runs}   Failed calls: {failures}

| Stage (ms) | median | p90 | p95 | max | samples |
|---|---|---|---|---|---|
{row('Agent turn (LLM + tools + DB)', agent)}
{row('TTS synthesis', tts)}

Note: record GPU/VRAM, Ollama version, model tag/quantisation and network conditions by hand in docs/evaluation-report.md.""")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--runs", type=int, default=10)
    asyncio.run(main(p.parse_args().runs))

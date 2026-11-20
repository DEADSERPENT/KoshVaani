"""Live round-trip check against the REAL speech providers (no mocks).

    backend\\.venv\\Scripts\\python.exe scripts\\live_speech_check.py

Needs GROQ_API_KEY and DEEPGRAM_API_KEY in .env. It speaks a sentence with Deepgram TTS, feeds that audio
into Groq Whisper STT, and compares the transcript. It writes check_output.mp3 so you can listen to it.
"""
import asyncio
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.config import get_settings  # noqa: E402
from app.core.errors import AppError  # noqa: E402
from app.providers.deepgram_tts import DeepgramTTS  # noqa: E402
from app.providers.groq_stt import GroqWhisperSTT  # noqa: E402
from app.voice.transcript_normalizer import parse_amount_paise  # noqa: E402

SENTENCE = "I sent four thousand two hundred and fifty rupees yesterday, but the recipient has not received it."


def words(s: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", s.lower())


async def main() -> int:
    s = get_settings()
    try:
        tts, stt = DeepgramTTS(s), GroqWhisperSTT(s)
    except AppError as e:
        print(f"FAIL: {e.message} Put the key in .env (see .env.example).")
        return 1
    try:
        t0 = time.perf_counter()
        audio = await tts.synthesize(SENTENCE)
        t1 = time.perf_counter()
        Path("check_output.mp3").write_bytes(audio.audio)
        print(f"TTS ok  provider={audio.provider} bytes={len(audio.audio)} request_id={audio.request_id} "
              f"{round((t1 - t0) * 1000)} ms -> check_output.mp3")
        res = await stt.transcribe(audio.audio, content_type="audio/mpeg", language=s.speech_language)
        t2 = time.perf_counter()
    except AppError as e:
        print(f"FAIL: {e.code}: {e.message}")
        return 1
    print(f"STT ok  provider={res.provider} request_id={res.request_id} {round((t2 - t1) * 1000)} ms")
    print(f"transcript: {res.text}")
    ref, hyp = words(SENTENCE), words(res.text)
    # word-level edit distance -> WER on this single sample (not a benchmark)
    d = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        prev, d[0] = d[0], i
        for j, h in enumerate(hyp, 1):
            prev, d[j] = d[j], min(d[j] + 1, d[j - 1] + 1, prev + (r != h))
    print(f"WER on this sample: {d[-1] / len(ref):.2%}   amount parsed: {parse_amount_paise(res.text)} paise (expect 425000)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

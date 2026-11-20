from app.config import Settings


def create_llm(settings: Settings):
    if settings.llm_provider == "ollama":
        from app.providers.guarded_llm import GuardedLLM
        from app.providers.ollama_llm import OllamaLLM

        return GuardedLLM(OllamaLLM(settings))
    from app.providers.rules_llm import RulesLLM

    return RulesLLM()


def create_stt(settings: Settings):
    if settings.stt_provider == "groq":
        from app.providers.groq_stt import GroqWhisperSTT

        return GroqWhisperSTT(settings)
    from app.providers.mock_speech import MockSTT

    return MockSTT()


def create_tts(settings: Settings):
    if settings.tts_provider == "deepgram":
        from app.providers.deepgram_tts import DeepgramTTS

        return DeepgramTTS(settings)
    from app.providers.mock_speech import MockTTS

    return MockTTS()

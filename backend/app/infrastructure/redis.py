from app.config import Settings


def create_redis(settings: Settings):
    if settings.storage_backend == "mongo":
        import redis.asyncio as aioredis

        return aioredis.from_url(settings.redis_url, decode_responses=True)
    import fakeredis

    return fakeredis.FakeAsyncRedis(decode_responses=True)

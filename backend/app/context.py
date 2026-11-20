from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from app.config import Settings
from app.core.crypto import FieldCipher


@dataclass
class AppContext:
    settings: Settings
    db: Any
    redis: Any
    cipher: FieldCipher | None = None
    llm: Any = None
    stt: Any = None
    tts: Any = None
    otp_sender: Any = None
    clock: Callable[[], datetime] | None = None

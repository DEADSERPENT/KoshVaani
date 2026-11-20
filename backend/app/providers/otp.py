import json
import logging

import httpx

from app.config import Settings
from app.core.errors import AppError

log = logging.getLogger("otp")


class DevScreenSender:
    """Development only: nothing is sent; the code is echoed by the API when ECHO_DEV_OTP is on."""
    name = "dev_screen"
    delivery_mode = "DEV_UI_ONLY"

    async def send(self, phone_e164: str, code: str) -> None:
        return None


class HttpSmsSender:
    """Posts a JSON body to an SMS gateway you configure, so any provider with an HTTP API can be used
    (MSG91, Twilio, a bank's own gateway, or a small relay in front of one).

    SMS_BODY_TEMPLATE is a JSON document with {phone} and {code} placeholders, for example
    {"to": "{phone}", "message": "Your KoshVaani code is {code}. Do not share it."}.
    The code and number are never logged, and the gateway's response body is never echoed to callers."""
    name = "http_sms"
    delivery_mode = "SMS"

    def __init__(self, settings: Settings):
        if not (settings.sms_url and settings.sms_body_template):
            raise ValueError("SMS_URL and SMS_BODY_TEMPLATE are required for OTP_PROVIDER=http")
        self.url = settings.sms_url
        self.template = settings.sms_body_template
        self.timeout = settings.speech_timeout_seconds
        self.headers = {"Content-Type": "application/json"}
        if settings.sms_auth_header_value:
            self.headers[settings.sms_auth_header_name] = settings.sms_auth_header_value

    async def send(self, phone_e164: str, code: str) -> None:
        body = json.loads(self.template.replace("{phone}", json.dumps(phone_e164)[1:-1])
                          .replace("{code}", json.dumps(code)[1:-1]))
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as c:
                r = await c.post(self.url, json=body, headers=self.headers)
            r.raise_for_status()
        except httpx.HTTPError as exc:
            raise AppError("PROVIDER_UNAVAILABLE", "OTP delivery failed.") from exc


def create_otp_sender(settings: Settings):
    if settings.auth_mode == "otp" and settings.otp_provider == "http":
        return HttpSmsSender(settings)
    return DevScreenSender()  # password mode never sends a code, so no SMS gateway is needed or built

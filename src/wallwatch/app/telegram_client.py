from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Protocol
from urllib import request as urllib_request
from urllib import error as urllib_error


class TelegramApiError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        description: str | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.description = description
        self.status_code = status_code


class TelegramHttpClient(Protocol):
    async def post_json(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError


DEFAULT_TELEGRAM_READ_TIMEOUT = 45


class UrllibTelegramHttpClient:
    def __init__(self, *, read_timeout: int = DEFAULT_TELEGRAM_READ_TIMEOUT) -> None:
        self._read_timeout = read_timeout

    async def post_json(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        data = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json; charset=utf-8"}
        req = urllib_request.Request(url, data=data, headers=headers)

        def _do_request() -> dict[str, Any]:
            try:
                with urllib_request.urlopen(req, timeout=self._read_timeout) as response:
                    payload_data = response.read()
                    return json.loads(payload_data.decode("utf-8"))
            except urllib_error.HTTPError as exc:
                description = _extract_description(exc)
                raise TelegramApiError(
                    f"HTTP {exc.code}", description=description, status_code=exc.code
                ) from exc

        return await asyncio.to_thread(_do_request)


class TelegramApiClient:
    def __init__(self, token: str, http_client: TelegramHttpClient, logger: logging.Logger) -> None:
        self._token = token
        self._client = http_client
        self._logger = logger

    async def get_updates(self, offset: int | None, timeout: int) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        payload = await self._client.post_json(self._url("/getUpdates"), params)
        if not payload.get("ok"):
            raise RuntimeError("telegram getUpdates failed")
        return payload.get("result", [])

    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        parse_mode: str | None = None,
        disable_web_preview: bool = True,
    ) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": disable_web_preview,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        await self._client.post_json(self._url("/sendMessage"), payload)

    def _url(self, path: str) -> str:
        return f"https://api.telegram.org/bot{self._token}{path}"


def _extract_description(exc: urllib_error.HTTPError) -> str | None:
    try:
        body = exc.read()
    except Exception:  # noqa: BLE001
        return None
    if not body:
        return None
    try:
        payload = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError:
        return body.decode("utf-8", errors="replace")
    if isinstance(payload, dict):
        description = payload.get("description")
        return str(description) if description else None
    return None

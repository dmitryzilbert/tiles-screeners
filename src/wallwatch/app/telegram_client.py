from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Protocol
from urllib import request as urllib_request


class TelegramHttpClient(Protocol):
    async def post_json(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError


class UrllibTelegramHttpClient:
    async def post_json(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        data = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        req = urllib_request.Request(url, data=data, headers=headers)

        def _do_request() -> dict[str, Any]:
            with urllib_request.urlopen(req, timeout=30) as response:
                if response.status >= 400:
                    raise RuntimeError(f"HTTP {response.status}")
                payload_data = response.read()
                return json.loads(payload_data.decode("utf-8"))

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

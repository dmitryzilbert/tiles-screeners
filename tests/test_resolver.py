from __future__ import annotations

import asyncio
import logging

from t_tech.invest import schemas

from wallwatch.api.client import MarketDataClient


class FakeInstrumentsService:
    def __init__(
        self,
        find_response: schemas.FindInstrumentResponse,
        instrument_response: schemas.InstrumentResponse,
    ) -> None:
        self._find_response = find_response
        self._instrument_response = instrument_response
        self.last_id_type: schemas.InstrumentIdType | None = None
        self.last_id: str | None = None

    async def find_instrument(self, *, query: str) -> schemas.FindInstrumentResponse:
        return self._find_response

    async def get_instrument_by(
        self,
        *,
        id_type: schemas.InstrumentIdType,
        class_code: str = "",
        id: str = "",
    ) -> schemas.InstrumentResponse:
        self.last_id_type = id_type
        self.last_id = id
        return self._instrument_response


def test_resolve_symbol_fetches_full_instrument_min_price_increment() -> None:
    instrument_short = schemas.InstrumentShort(
        uid="uid-123",
        ticker="SBER",
        instrument_kind=schemas.InstrumentType.INSTRUMENT_TYPE_SHARE,
        api_trade_available_flag=True,
    )
    full_instrument = schemas.Instrument(
        uid="uid-123",
        min_price_increment=schemas.Quotation(units=0, nano=10_000_000),
    )
    service = FakeInstrumentsService(
        schemas.FindInstrumentResponse(instruments=[instrument_short]),
        schemas.InstrumentResponse(instrument=full_instrument),
    )
    client = MarketDataClient(token="token", logger=logging.getLogger("test"))

    info = asyncio.run(client._resolve_symbol(service, "SBER"))

    assert info is not None
    assert info.instrument_id == "uid-123"
    assert info.tick_size == 0.01
    assert service.last_id_type == schemas.InstrumentIdType.INSTRUMENT_ID_TYPE_UID
    assert service.last_id == "uid-123"

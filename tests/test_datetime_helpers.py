from __future__ import annotations

from datetime import datetime, timezone

from wallwatch.api.client import to_datetime


class FakeTimestamp:
    def __init__(self, value: datetime) -> None:
        self._value = value

    def ToDatetime(self) -> datetime:
        return self._value


def test_to_datetime_with_datetime() -> None:
    value = datetime.now(timezone.utc)

    assert to_datetime(value) is value


def test_to_datetime_with_to_datetime_method() -> None:
    value = datetime.now(timezone.utc)
    fake = FakeTimestamp(value)

    assert to_datetime(fake) is value


def test_to_datetime_with_none() -> None:
    assert to_datetime(None) is None

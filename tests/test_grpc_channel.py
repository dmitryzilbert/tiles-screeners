from __future__ import annotations

import grpc

from wallwatch.api.client import create_grpc_channel


def test_create_grpc_channel_uses_custom_root_certificates() -> None:
    captured: dict[str, object] = {}

    def fake_ssl_channel_credentials(root_certificates=None):  # type: ignore[no-untyped-def]
        captured["root_certificates"] = root_certificates
        return "creds"

    def fake_secure_channel(*args, **kwargs):  # type: ignore[no-untyped-def]
        captured["secure_args"] = args
        captured["secure_kwargs"] = kwargs
        return "channel"

    original_ssl = grpc.ssl_channel_credentials
    original_secure = grpc.secure_channel
    try:
        grpc.ssl_channel_credentials = fake_ssl_channel_credentials  # type: ignore[assignment]
        grpc.secure_channel = fake_secure_channel  # type: ignore[assignment]
        channel = create_grpc_channel(target="invest.example:443", root_certificates=b"pem")
    finally:
        grpc.ssl_channel_credentials = original_ssl  # type: ignore[assignment]
        grpc.secure_channel = original_secure  # type: ignore[assignment]

    assert channel == "channel"
    assert captured["root_certificates"] == b"pem"
    assert captured["secure_args"][1] == "creds"


def test_create_grpc_channel_uses_custom_root_certificates_async() -> None:
    captured: dict[str, object] = {}

    def fake_ssl_channel_credentials(root_certificates=None):  # type: ignore[no-untyped-def]
        captured["root_certificates"] = root_certificates
        return "creds"

    def fake_secure_channel(*args, **kwargs):  # type: ignore[no-untyped-def]
        captured["secure_args"] = args
        captured["secure_kwargs"] = kwargs
        return "channel"

    original_ssl = grpc.ssl_channel_credentials
    original_secure = grpc.aio.secure_channel
    try:
        grpc.ssl_channel_credentials = fake_ssl_channel_credentials  # type: ignore[assignment]
        grpc.aio.secure_channel = fake_secure_channel  # type: ignore[assignment]
        channel = create_grpc_channel(
            target="invest.example:443",
            root_certificates=b"pem",
            force_async=True,
        )
    finally:
        grpc.ssl_channel_credentials = original_ssl  # type: ignore[assignment]
        grpc.aio.secure_channel = original_secure  # type: ignore[assignment]

    assert channel == "channel"
    assert captured["root_certificates"] == b"pem"
    assert captured["secure_args"][1] == "creds"

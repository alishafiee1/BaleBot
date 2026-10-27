import httpx
import pytest

from balebot.net import safe_http
from balebot.net.safe_http import SafeHttp, UnsafeURL, _public, check_url, resolve_public
from balebot.services.providers import (
    ProviderConfig,
    extract_models,
    parse_generated,
    parse_site,
    same_site,
)
from balebot.vault import Vault, VaultError, key_aad


def test_vault_roundtrip_and_binding() -> None:
    v = Vault.from_b64(Vault.generate_key())
    blob = v.encrypt("sk-secret", key_aad(1, 10))
    assert b"sk-secret" not in blob
    assert v.decrypt(blob, key_aad(1, 10)) == "sk-secret"
    with pytest.raises(VaultError):
        v.decrypt(blob, key_aad(2, 10))  # another user's row never decrypts
    with pytest.raises(VaultError):
        v.decrypt(blob[:-1] + bytes([blob[-1] ^ 1]), key_aad(1, 10))  # tampered
    with pytest.raises(VaultError):
        Vault.from_b64(Vault.generate_key()).decrypt(blob, key_aad(1, 10))  # wrong master key


def test_vault_rejects_short_key() -> None:
    with pytest.raises(ValueError):
        Vault(b"short")


@pytest.mark.parametrize(
    "url",
    [
        "http://api.example.com/v1",
        "https://user:pw@api.example.com/v1",
        "https://api.example.com:8443/v1",
        "ftp://api.example.com",
    ],
)
def test_check_url_rejects(url: str) -> None:
    with pytest.raises(UnsafeURL):
        check_url(url)


@pytest.mark.parametrize(
    ("ip", "public"),
    [
        ("93.184.216.34", True),
        ("127.0.0.1", False),
        ("10.0.0.5", False),
        ("192.168.1.1", False),
        ("169.254.169.254", False),
        ("::1", False),
        ("::ffff:127.0.0.1", False),
        ("fd42::1", False),
    ],
)
def test_public_ip_rules(ip: str, public: bool) -> None:
    assert _public(ip) is public


async def test_resolve_blocks_localhost_and_literals() -> None:
    with pytest.raises(UnsafeURL):
        await resolve_public("localhost")
    with pytest.raises(UnsafeURL):
        await resolve_public("127.0.0.1")
    with pytest.raises(UnsafeURL):
        await resolve_public("[::1]")


async def test_request_is_pinned_to_checked_ip(provider) -> None:
    pins = []

    def handler(request: httpx.Request) -> httpx.Response:
        pins.append(safe_http._pin.get())
        return provider.handler(request)

    http = SafeHttp(transport=httpx.MockTransport(handler))
    await http.request("GET", "https://api.example.com/v1/models")
    # the URL keeps the real hostname (TLS name check); TCP is pinned to the checked address
    assert provider.seen[0].url.host == "api.example.com"
    assert pins == [("api.example.com", "93.184.216.34")]
    assert safe_http._pin.get() is None  # pin does not leak past the request


async def test_pinned_backend_refuses_unchecked_host() -> None:
    import httpcore

    backend = safe_http._PinnedBackend()
    with pytest.raises(httpcore.ConnectError):
        await backend.connect_tcp("evil.example", 443)  # no pin set
    token = safe_http._pin.set(("good.example", "93.184.216.34"))
    try:
        with pytest.raises(httpcore.ConnectError):
            await backend.connect_tcp("other.example", 443)  # pinned for a different host
    finally:
        safe_http._pin.reset(token)


def test_real_transport_can_be_built() -> None:
    SafeHttp()  # fails if the pinning hook no longer matches httpx internals


def test_parse_site() -> None:
    assert parse_site("example.com") == "https://example.com"
    assert parse_site("https://docs.example.com/x") == "https://docs.example.com/x"
    assert parse_site("not a site") is None
    assert parse_site("openai") is None


def test_same_site() -> None:
    assert same_site("api.example.com", "docs.example.com")
    assert not same_site("api.evil.com", "docs.example.com")


GOOD = '{"name":"Ex","base_url":"https://api.example.com/v1","auth":"bearer"}'


def test_generated_config_must_match_named_site() -> None:
    assert parse_generated("sure: " + GOOD, "docs.example.com").host == "api.example.com"
    evil = GOOD.replace("api.example.com", "attacker.net")
    with pytest.raises(ValueError):
        parse_generated(evil, "docs.example.com")  # injected host from a poisoned docs page


@pytest.mark.parametrize(
    "bad",
    [
        '{"name":"x","base_url":"http://api.example.com/v1"}',
        '{"name":"x","base_url":"https://api.example.com/v1","chat_path":"/../admin"}',
        '{"name":"x","base_url":"https://api.example.com/v1","chat_path":"//evil.com"}',
        '{"error":"unsupported"}',
        "no json here",
    ],
)
def test_generated_config_rejects_bad(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_generated(bad, "example.com")


def test_provider_config_paths() -> None:
    with pytest.raises(ValueError):
        ProviderConfig(name="x", base_url="https://a.example.com", chat_path="chat")


def test_extract_models() -> None:
    assert extract_models(b'{"data":[{"id":"b"},{"id":"a"}]}') == ["a", "b"]
    assert extract_models(b'{"models":[{"name":"models/gemini"}]}') == ["gemini"]
    assert extract_models(b"garbage") == []


async def test_request_fails_over_to_second_ip_on_timeout(
    provider, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def multi_resolve(host: str) -> list[str]:
        return ["93.184.216.34", "93.184.216.35"]

    monkeypatch.setattr(safe_http, "resolve_public", multi_resolve)
    pins: list[tuple[str, str] | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        pin = safe_http._pin.get()
        pins.append(pin)
        if pin and pin[1] == "93.184.216.34":
            raise httpx.ReadTimeout("stalled first ip")
        return provider.handler(request)

    http = SafeHttp(transport=httpx.MockTransport(handler))
    res = await http.request("GET", "https://api.example.com/")
    assert res.status == 200
    assert pins == [
        ("api.example.com", "93.184.216.34"),
        ("api.example.com", "93.184.216.35"),
    ]

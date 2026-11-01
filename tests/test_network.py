"""Proxy of the update check and of model downloads (``anonyma/network.py``)."""

from __future__ import annotations

import http.server
import ssl
import threading
from collections.abc import Iterator
from typing import Any

import pytest

from anonyma import model_manager, netguard, network, updater
from anonyma.config import Config
from anonyma.errors import network_reason

PROXY_VARS = ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY")


@pytest.fixture
def no_system_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in PROXY_VARS:
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)
    monkeypatch.setattr(network, "_windows_proxy", lambda *_a: None)
    monkeypatch.setattr(network.urllib.request, "getproxies", dict)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", ""),
        ("  ", ""),
        ("proxy.exemple.fr:8080", "http://proxy.exemple.fr:8080"),
        ("http://proxy.exemple.fr:3128/", "http://proxy.exemple.fr:3128"),
        ("https://u:p@10.0.0.1:443", "https://u:p@10.0.0.1:443"),
    ],
)
def test_normalize_proxy(raw: str, expected: str) -> None:
    assert network.normalize_proxy(raw) == expected


@pytest.mark.parametrize(
    "raw", ["proxy.exemple.fr", "socks5://p:1080", "http://p:8080/chemin", "http://:8080"]
)
def test_normalize_proxy_rejects(raw: str) -> None:
    with pytest.raises(ValueError, match="Proxy"):
        network.normalize_proxy(raw)


def test_password_never_shown() -> None:
    config = Config(proxy="http://agent:s3cret@proxy.exemple.fr:8080")
    config.validate()
    assert "s3cret" not in repr(config)
    shown = network.redact_proxy(config.proxy)
    assert shown == "http://agent:***@proxy.exemple.fr:8080"
    assert network.redact_proxy("http://p.exemple.fr:8080") == "http://p.exemple.fr:8080"


def test_config_rejects_an_invalid_proxy() -> None:
    with pytest.raises(ValueError, match="Proxy"):
        Config.from_dict({"proxy": "ftp://p:21"})


def test_configured_proxy_wins(no_system_proxy: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://systeme.exemple.fr:1")
    assert network.resolve_proxy("https://api.github.com", "p.exemple.fr:8080") == (
        "http://p.exemple.fr:8080"
    )
    assert network.resolve_proxy("https://api.github.com") == "http://systeme.exemple.fr:1"


def test_environment_and_exceptions(no_system_proxy: None, monkeypatch: pytest.MonkeyPatch) -> None:
    assert network.resolve_proxy("https://huggingface.co") is None
    monkeypatch.setenv("HTTPS_PROXY", "systeme.exemple.fr:3128")
    assert network.resolve_proxy("https://huggingface.co") == "http://systeme.exemple.fr:3128"
    monkeypatch.setenv("NO_PROXY", "huggingface.co")
    assert network.resolve_proxy("https://huggingface.co") is None
    assert network.resolve_proxy("https://api.github.com") == "http://systeme.exemple.fr:3128"


def test_broken_system_setting_goes_direct(
    no_system_proxy: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(_url: str) -> str:
        raise OSError("registre illisible")

    monkeypatch.setattr(network, "system_proxy", broken)
    assert network.resolve_proxy("https://api.github.com") is None


@pytest.mark.parametrize(
    ("spec", "scheme", "expected"),
    [
        ("p1:8080", "https", "p1:8080"),
        ("p1:8080;p2:8080", "https", "p1:8080"),
        ("p1:8080 p2:8080", "https", "p1:8080"),
        ("http=p1:80;https=p2:443", "https", "p2:443"),
        ("http=p1:80", "https", "p1:80"),
        ("ftp=p1:21", "https", None),
    ],
)
def test_windows_proxy_lists(spec: str, scheme: str, expected: str | None) -> None:
    assert network._pick(spec, scheme) == expected


def test_windows_settings_never_crash() -> None:
    """The real WinHTTP lookup of this machine: an address or direct, never an error."""
    found = network.resolve_proxy("https://api.github.com")
    assert found is None or found.startswith(("http://", "https://"))


class _ProxyHandler(http.server.BaseHTTPRequestHandler):
    seen: list[str] = []

    def do_GET(self) -> None:
        type(self).seen.append(self.path)
        body = b'{"via": "proxy"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: Any) -> None:
        pass


@pytest.fixture
def local_proxy() -> Iterator[str]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _ProxyHandler)
    _ProxyHandler.seen = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def test_requests_go_through_the_proxy(local_proxy: str) -> None:
    """The host is never resolved here: the proxy receives the full address."""
    netguard.install()
    url = "http://depot.exemple.invalid/releases/latest"
    with netguard.allow_network(), network.client(url, local_proxy, timeout=5) as client:
        assert client.get(url).json() == {"via": "proxy"}
    assert _ProxyHandler.seen == [url]


def test_updater_uses_the_configured_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []

    class Response:
        status_code = 404

    class Client:
        def __enter__(self) -> Client:
            return self

        def __exit__(self, *_a: object) -> None:
            pass

        def get(self, _url: str) -> Response:
            return Response()

    def fake(url: str, _timeout: float, proxy: str) -> Client:
        calls.append((url, proxy))
        return Client()

    monkeypatch.setattr(updater, "_client", fake)
    assert updater.check_latest(proxy="http://p.exemple.fr:8080") is None
    assert calls == [(updater.LATEST_RELEASE_URL, "http://p.exemple.fr:8080")]


def test_hugging_face_client_follows_the_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    import huggingface_hub

    factories: list[object] = []
    monkeypatch.setattr(huggingface_hub, "set_client_factory", factories.append)
    monkeypatch.setattr(model_manager, "_hf_proxy", None)
    with model_manager._online("http://p.exemple.fr:8080"):
        pass
    with model_manager._online("http://p.exemple.fr:8080"):
        pass
    assert len(factories) == 1  # same proxy: the shared client is kept
    with model_manager._online("http://autre.exemple.fr:8080"):
        pass
    assert len(factories) == 2
    client = factories[-1]()  # type: ignore[operator]
    try:
        assert client.event_hooks["request"]  # huggingface_hub's offline check kept
    finally:
        client.close()


def test_network_reasons_are_french() -> None:
    import httpx2

    assert "proxy" in network_reason(httpx2.ProxyError("407 Proxy Authentication Required"))
    try:
        try:
            raise ssl.SSLCertVerificationError("unable to get local issuer certificate")
        except ssl.SSLError as inner:
            raise httpx2.ConnectError("[SSL]") from inner
    except httpx2.ConnectError as exc:
        assert network_reason(exc) == "certificat du serveur non reconnu par ce poste"
    assert network_reason(httpx2.ConnectTimeout("t")) == "délai de connexion dépassé"

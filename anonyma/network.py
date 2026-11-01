"""HTTP clients of the only network accesses: update check and download, model download.

Professional workstations often reach the Internet through a proxy, sometimes with a
company certificate that inspects TLS traffic. Every client built here:

- goes through the proxy set in the settings (``Config.proxy``) when there is one;
- otherwise through the proxy of the system, as a browser would: the
  ``HTTPS_PROXY`` / ``HTTP_PROXY`` / ``ALL_PROXY`` / ``NO_PROXY`` variables, then on
  Windows the Internet settings (automatic detection, configuration script (PAC),
  manual proxy and its exceptions) resolved by WinHTTP, on macOS the network
  preferences; no proxy at all goes direct;
- checks certificates against the certificate store of the system (``truststore``
  through httpx2), so a company root certificate trusted by the browser is trusted
  here too.

The proxy address is never logged: it may carry a user name and a password.
"""

from __future__ import annotations

import ctypes
import sys
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

from anonyma.logs import get_logger

log = get_logger("network")

PROXY_SCHEMES = ("http", "https")
#: Upper bound of each WinHTTP step of the proxy lookup (WPAD, PAC download), in ms.
_WINHTTP_TIMEOUT_MS = 5000


def normalize_proxy(value: str) -> str:
    """``"proxy.example:8080"`` -> ``"http://proxy.example:8080"``; ``""`` stays empty.

    Raises ValueError (French) when the address is not a usable HTTP proxy."""
    text = value.strip()
    if not text:
        return ""
    if "://" not in text:
        text = f"http://{text}"
    parts = urlsplit(text)
    if parts.scheme.lower() not in PROXY_SCHEMES:
        raise ValueError("Proxy : seuls les proxys http:// et https:// sont pris en charge.")
    try:
        port = parts.port
    except ValueError:
        port = None
    if not parts.hostname or port is None:
        raise ValueError("Proxy : indiquez une adresse et un port, ex. proxy.entreprise.fr:8080.")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ValueError("Proxy : l'adresse ne doit contenir ni chemin ni paramètres.")
    return text.rstrip("/")


def redact_proxy(value: str) -> str:
    """The proxy address without its password, for display."""
    parts = urlsplit(value.strip())
    if parts.password is None:
        return value
    host = parts.netloc.rpartition("@")[2]
    return parts._replace(netloc=f"{parts.username}:***@{host}").geturl()


def resolve_proxy(url: str, configured: str = "") -> str | None:
    """Proxy to reach ``url``: the configured one, else the system's; None = direct."""
    if configured.strip():
        return normalize_proxy(configured)
    try:
        found = system_proxy(url)
        return normalize_proxy(found) if found else None
    except Exception as exc:  # a broken system setting must not prevent a direct try
        log.info("system proxy lookup failed: %s", type(exc).__name__)
        return None


def system_proxy(url: str) -> str | None:
    """Proxy the system uses for ``url`` (environment, then system settings)."""
    parts = urlsplit(url)
    scheme, host = parts.scheme.lower(), parts.hostname or ""
    env = urllib.request.getproxies_environment()
    if env.get("no") and _bypassed_by("proxy_bypass_environment", host, env):
        return None
    if found := env.get(scheme) or env.get("all"):
        return found
    if sys.platform == "win32":
        return _windows_proxy(url, scheme, host)
    proxies = urllib.request.getproxies()  # macOS: network preferences
    if not (found := proxies.get(scheme) or proxies.get("all")):
        return None
    return None if urllib.request.proxy_bypass(host) else found


def _bypassed_by(name: str, *args: Any) -> bool:
    """``urllib.request`` exceptions check missing from its stubs (``NO_PROXY``, the
    exceptions of the Windows Internet settings)."""
    check: Callable[..., Any] = getattr(urllib.request, name)
    return bool(check(*args))


def _pick(spec: str, scheme: str) -> str | None:
    """First proxy of a WinHTTP list: ``"a:8080;b:8080"`` or ``"http=a:1;https=b:2"``."""
    entries = [e.strip() for e in spec.replace(" ", ";").split(";") if e.strip()]
    if not any("=" in e for e in entries):
        return entries[0] if entries else None
    by_scheme = dict(e.split("=", 1) for e in entries if "=" in e)
    return by_scheme.get(scheme) or by_scheme.get("http") or None


def _windows_proxy(url: str, scheme: str, host: str) -> str | None:
    config = _ie_proxy_config()
    if config is None:
        return None
    auto_detect, script, manual = config
    if auto_detect or script:
        found = _winhttp_auto_proxy(url, script, auto_detect)
        if found is not None:  # "" = the script says DIRECT
            return _pick(found, scheme) if found else None
    if manual and not _bypassed_by("proxy_bypass_registry", host):
        return _pick(manual, scheme)
    return None


# -- WinHTTP (Windows only) ----------------------------------------------------------

_ACCESS_NO_PROXY = 1
_ACCESS_NAMED_PROXY = 3
_AUTOPROXY_AUTO_DETECT = 0x1
_AUTOPROXY_CONFIG_URL = 0x2
_AUTO_DETECT_DHCP = 0x1
_AUTO_DETECT_DNS_A = 0x2
_ERROR_LOGIN_FAILURE = 12015


class _IEProxyConfig(ctypes.Structure):
    _fields_ = (
        ("fAutoDetect", ctypes.c_int),
        ("lpszAutoConfigUrl", ctypes.c_void_p),
        ("lpszProxy", ctypes.c_void_p),
        ("lpszProxyBypass", ctypes.c_void_p),
    )


class _AutoProxyOptions(ctypes.Structure):
    _fields_ = (
        ("dwFlags", ctypes.c_ulong),
        ("dwAutoDetectFlags", ctypes.c_ulong),
        ("lpszAutoConfigUrl", ctypes.c_wchar_p),
        ("lpvReserved", ctypes.c_void_p),
        ("dwReserved", ctypes.c_ulong),
        ("fAutoLogonIfChallenged", ctypes.c_int),
    )


class _ProxyInfo(ctypes.Structure):
    _fields_ = (
        ("dwAccessType", ctypes.c_ulong),
        ("lpszProxy", ctypes.c_void_p),
        ("lpszProxyBypass", ctypes.c_void_p),
    )


def _dll(name: str) -> Any:
    loader: Callable[..., Any] = getattr(ctypes, "WinDLL")  # noqa: B009 (Windows only)
    return loader(name, use_last_error=True)


def _last_error() -> int:
    """``ctypes.get_last_error()`` (Windows only, like :func:`_dll`)."""
    getter: Callable[[], int] = getattr(ctypes, "get_last_error")  # noqa: B009
    return getter()


def _take(pointer: int | None) -> str:
    """Read and free a string WinHTTP allocated with ``GlobalAlloc``."""
    if not pointer:
        return ""
    try:
        return ctypes.wstring_at(pointer)
    finally:
        kernel32 = _dll("kernel32")
        kernel32.GlobalFree.argtypes = (ctypes.c_void_p,)
        kernel32.GlobalFree(pointer)


def _ie_proxy_config() -> tuple[bool, str, str] | None:
    """(automatic detection, configuration script URL, manual proxy) of the user."""
    winhttp = _dll("winhttp")
    config = _IEProxyConfig()
    if not winhttp.WinHttpGetIEProxyConfigForCurrentUser(ctypes.byref(config)):
        return None
    script = _take(config.lpszAutoConfigUrl)
    manual = _take(config.lpszProxy)
    _take(config.lpszProxyBypass)  # read through the registry by urllib
    return bool(config.fAutoDetect), script, manual


def _winhttp_auto_proxy(url: str, script: str, auto_detect: bool) -> str | None:
    """Proxy list from WPAD / the PAC script for ``url``; "" = direct, None = unknown."""
    winhttp = _dll("winhttp")
    winhttp.WinHttpOpen.restype = ctypes.c_void_p
    winhttp.WinHttpOpen.argtypes = (
        ctypes.c_wchar_p,
        ctypes.c_ulong,
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_ulong,
    )
    winhttp.WinHttpSetTimeouts.argtypes = (ctypes.c_void_p,) + (ctypes.c_int,) * 4
    winhttp.WinHttpGetProxyForUrl.argtypes = (
        ctypes.c_void_p,
        ctypes.c_wchar_p,
        ctypes.POINTER(_AutoProxyOptions),
        ctypes.POINTER(_ProxyInfo),
    )
    winhttp.WinHttpCloseHandle.argtypes = (ctypes.c_void_p,)
    session = winhttp.WinHttpOpen("Anonyma", _ACCESS_NO_PROXY, None, None, 0)
    if not session:
        return None
    try:
        t = _WINHTTP_TIMEOUT_MS
        winhttp.WinHttpSetTimeouts(session, t, t, t, t)
        options = _AutoProxyOptions()
        if script:
            options.dwFlags = _AUTOPROXY_CONFIG_URL
            options.lpszAutoConfigUrl = script
        else:
            options.dwFlags = _AUTOPROXY_AUTO_DETECT
            options.dwAutoDetectFlags = _AUTO_DETECT_DHCP | _AUTO_DETECT_DNS_A
        info = _ProxyInfo()
        ok = winhttp.WinHttpGetProxyForUrl(session, url, ctypes.byref(options), ctypes.byref(info))
        if not ok and _last_error() == _ERROR_LOGIN_FAILURE:
            options.fAutoLogonIfChallenged = 1  # the script is behind Windows logon
            ok = winhttp.WinHttpGetProxyForUrl(
                session, url, ctypes.byref(options), ctypes.byref(info)
            )
        if not ok and script and auto_detect:
            options.dwFlags |= _AUTOPROXY_AUTO_DETECT
            options.dwAutoDetectFlags = _AUTO_DETECT_DHCP | _AUTO_DETECT_DNS_A
            ok = winhttp.WinHttpGetProxyForUrl(
                session, url, ctypes.byref(options), ctypes.byref(info)
            )
        if not ok:
            return None
        proxy = _take(info.lpszProxy)
        _take(info.lpszProxyBypass)
        return proxy if info.dwAccessType == _ACCESS_NAMED_PROXY else ""
    finally:
        winhttp.WinHttpCloseHandle(session)


# -- clients ---------------------------------------------------------------------------


def client(
    url: str,
    configured_proxy: str = "",
    *,
    timeout: float | None,
    headers: Mapping[str, str] | None = None,
    event_hooks: Mapping[str, list[Callable[..., Any]]] | None = None,
) -> Any:
    """An ``httpx2.Client`` for ``url`` and the hosts it redirects to (same proxy)."""
    import httpcore2
    import httpx2

    return httpx2.Client(
        proxy=resolve_proxy(url, configured_proxy),
        # The proxy is resolved above: the environment must not override it.
        trust_env=False,
        verify=httpcore2.default_ssl_context(),
        follow_redirects=True,
        timeout=timeout,
        headers=dict(headers or {}),
        event_hooks={k: list(v) for k, v in (event_hooks or {}).items()},
    )


__all__ = ["client", "normalize_proxy", "redact_proxy", "resolve_proxy", "system_proxy"]

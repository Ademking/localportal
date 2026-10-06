"""The reverse proxy that powers localportal.

Every request that reaches the local server is replayed against the upstream
site with the upstream's Host header, and the response is sent back with the
few changes a browser needs to keep treating ``http://127.0.0.1:<port>`` as the
site: redirects and absolute links point back at the mirror, cookies lose their
Domain/Secure attributes, and headers that only make sense on the real HTTPS
origin (HSTS, CSP, Alt-Svc) are dropped.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import time
import webbrowser
from typing import Mapping, Optional

import aiohttp
from aiohttp import web
from multidict import CIMultiDict
from yarl import URL

log = logging.getLogger("localportal")

HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
)

# Request headers that are never forwarded as-is.
REQUEST_SKIP = HOP_BY_HOP | {"host", "content-length", "accept-encoding"}

# Handshake headers that aiohttp generates itself when opening the upstream socket.
WS_SKIP = REQUEST_SKIP | {
    "sec-websocket-key",
    "sec-websocket-version",
    "sec-websocket-extensions",
    "sec-websocket-protocol",
}

# Response headers that are wrong (bodies are decompressed) or that would break
# the mirror on plain http://127.0.0.1.
RESPONSE_SKIP = HOP_BY_HOP | {
    "content-length",
    "content-encoding",
    "strict-transport-security",
    "content-security-policy",
    "content-security-policy-report-only",
    "alt-svc",
    "public-key-pins",
}

# Response headers whose values can hold absolute upstream URLs.
URL_HEADERS = frozenset(
    {"location", "content-location", "link", "refresh", "access-control-allow-origin"}
)

REWRITE_MIME_TYPES = frozenset(
    {
        "application/javascript",
        "application/x-javascript",
        "application/ecmascript",
        "application/json",
        "application/xml",
        "application/xhtml+xml",
    }
)

HTML_MIME_TYPES = frozenset({"text/html", "application/xhtml+xml"})

INTEGRITY_ATTR = re.compile(rb"""\sintegrity\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)""", re.I)
META_CSP = re.compile(rb"""<meta\b[^>]*http-equiv\s*=\s*["']?content-security-policy[^>]*>""", re.I)

MAX_REQUEST_BODY = 1024**3
MAX_HEADER_SIZE = 64 * 1024


# What we ask the upstream for. Bodies are decompressed so they can be
# rewritten; br is left out because aiohttp's brotli support depends on which
# Brotli release happens to be installed (older ones fail mid-stream).
ACCEPT_ENCODING = "gzip, deflate"


def normalize_target(url: str) -> URL:
    """Turn ``example.com`` or ``https://example.com/path`` into a validated URL."""
    url = url.strip()
    if "://" not in url:
        url = "https://" + url
    parsed = URL(url)
    if parsed.scheme not in ("http", "https") or not parsed.host:
        raise ValueError(f"not a valid http(s) URL: {url!r}")
    return parsed


def _netloc(url: URL) -> str:
    host = (url.raw_host or "").lower()
    if ":" in host:
        host = f"[{host}]"
    return host if url.is_default_port() else f"{host}:{url.port}"


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return True


def _same_site(a: Optional[str], b: Optional[str]) -> bool:
    """True when two hosts differ at most by a leading ``www.``."""
    if not a or not b:
        return False
    a, b = a.lower(), b.lower()
    return a.removeprefix("www.") == b.removeprefix("www.")


def _mime_type(content_type: str) -> str:
    return content_type.split(";", 1)[0].strip().lower()


def _is_rewritable(mime: str) -> bool:
    if mime.startswith("text/"):
        return mime != "text/event-stream"
    return mime in REWRITE_MIME_TYPES or mime.endswith(("+json", "+xml"))


def _is_websocket(request: web.Request) -> bool:
    return (
        request.headers.get("Upgrade", "").lower() == "websocket"
        and "upgrade" in request.headers.get("Connection", "").lower()
    )


def _connection_tokens(headers: Mapping[str, str]) -> set:
    """Header names listed in ``Connection`` are hop-by-hop too."""
    return {t.strip().lower() for t in headers.get("Connection", "").split(",") if t.strip()}


def rewrite_set_cookie(value: str) -> str:
    """Make an upstream cookie stick to http://127.0.0.1.

    ``Domain`` is dropped so the cookie becomes host-only. ``Secure`` (and the
    ``SameSite=None``/``Partitioned`` attributes that require it) are dropped
    too, except on ``__Secure-``/``__Host-`` cookies, which browsers reject
    without ``Secure`` and accept on localhost anyway.
    """
    parts = [p.strip() for p in value.split(";")]
    name = parts[0].split("=", 1)[0].strip()
    keep_secure = name.startswith(("__Secure-", "__Host-"))
    out = [parts[0]]
    for attr in parts[1:]:
        if not attr:
            continue
        key, _, val = attr.partition("=")
        key = key.strip().lower()
        if key == "domain":
            continue
        if not keep_secure:
            if key in ("secure", "partitioned"):
                continue
            if key == "samesite" and val.strip().lower() == "none":
                attr = "SameSite=Lax"
        out.append(attr)
    return "; ".join(out)


def _ws_close_code(code: Optional[int]) -> int:
    # 1005/1006/1015 are reserved for reporting and must never be sent.
    if code and 1000 <= code < 5000 and code not in (1004, 1005, 1006, 1015):
        return code
    return 1000


class LocalPortal:
    """A local reverse proxy that serves ``target`` on ``http://host:port``.

    >>> LocalPortal("https://example.com", port=3000).run()
    """

    def __init__(
        self,
        target: str,
        host: str = "127.0.0.1",
        port: int = 3000,
        *,
        rewrite: bool = True,
        resolve: bool = True,
        verify_ssl: bool = True,
        forward_headers: bool = False,
        extra_headers: Optional[Mapping[str, str]] = None,
    ) -> None:
        self.target = normalize_target(target)
        self.host = host
        self.port = port
        self.rewrite = rewrite
        self.resolve = resolve
        self.verify_ssl = verify_ssl
        self.forward_headers = forward_headers
        self.extra_headers = dict(extra_headers or {})
        self.start_path = self.target.raw_path_qs if self.target.raw_path not in ("", "/") else ""
        self._session: Optional[aiohttp.ClientSession] = None
        self._set_upstream(self.target.origin())

    # ------------------------------------------------------------------ setup

    def _set_upstream(self, upstream: URL) -> None:
        self.upstream = upstream
        self.upstream_origin = str(upstream)
        hosts = {_netloc(self.target), _netloc(upstream)}
        for host in list(hosts):
            bare = host.split(":", 1)[0]
            if "." in bare and not _is_ip(bare):
                hosts.add(host[4:] if host.startswith("www.") else "www." + host)
        self.aliases = frozenset(hosts)
        alternatives = b"|".join(
            re.escape(h.encode("ascii"))
            for h in sorted(hosts, key=len, reverse=True)
        )
        # Matches https://host, http://host, //host and their JSON-escaped
        # forms (https:\/\/host), but not host.evil.com or host:8443.
        self._url_pattern = re.compile(
            rb"(?P<scheme>https?:)?(?P<slashes>//|\\/\\/)(?:"
            + alternatives
            + rb")(?::(?:80|443))?(?![\w-]|\.[\w-]|:\d)",
            re.I,
        )

    async def _resolve_upstream(self) -> None:
        """Follow the root redirect once, e.g. example.com -> https://www.example.com.

        Without this, a site that redirects to its canonical host would loop:
        the redirect gets rewritten back to the mirror, which asks the
        non-canonical host again.
        """
        try:
            async with self._session.get(
                self.target,
                allow_redirects=True,
                max_redirects=10,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                final = resp.url
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            log.warning("could not reach %s yet (%s); continuing anyway", self.target, exc)
            return
        if final.origin() != self.upstream and _same_site(final.host, self.target.host):
            log.info("following redirect: %s -> %s", self.upstream_origin, final.origin())
            self._set_upstream(final.origin())

    async def _session_ctx(self, app: web.Application):
        connector = aiohttp.TCPConnector(ssl=self.verify_ssl, limit=0)
        self._session = aiohttp.ClientSession(
            connector=connector,
            cookie_jar=aiohttp.DummyCookieJar(),
            auto_decompress=True,
            trust_env=True,
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=30),
            max_line_size=MAX_HEADER_SIZE,
            max_field_size=MAX_HEADER_SIZE,
        )
        if self.resolve:
            await self._resolve_upstream()
        yield
        await self._session.close()

    def make_app(self) -> web.Application:
        """Build the aiohttp application (useful for embedding or testing)."""
        app = web.Application(client_max_size=MAX_REQUEST_BODY)
        app.cleanup_ctx.append(self._session_ctx)
        app.router.add_route("*", "/{tail:.*}", self.handle)
        return app

    # -------------------------------------------------------------- rewriting

    @staticmethod
    def _local_origin(request: web.Request) -> str:
        return f"{request.scheme}://{request.host}"

    def _to_upstream(self, value: str, local: str) -> str:
        """Map a mirror URL (Origin/Referer) back to the real site."""
        if value == local or value.startswith(local + "/"):
            return self.upstream_origin + value[len(local):]
        return value

    def rewrite_bytes(self, data: bytes, local: str) -> bytes:
        """Point every absolute upstream URL in ``data`` at the mirror."""
        scheme, _, netloc = local.partition("://")
        scheme_b, netloc_b = scheme.encode() + b":", netloc.encode()

        def repl(match: "re.Match[bytes]") -> bytes:
            prefix = scheme_b if match.group("scheme") else b""
            return prefix + match.group("slashes") + netloc_b

        return self._url_pattern.sub(repl, data)

    def rewrite_text(self, value: str, local: str) -> str:
        raw = value.encode("utf-8", "surrogateescape")
        return self.rewrite_bytes(raw, local).decode("utf-8", "surrogateescape")

    def _rewrite_body(self, body: bytes, local: str, mime: str) -> bytes:
        body = self.rewrite_bytes(body, local)
        if mime in HTML_MIME_TYPES:
            # Rewritten scripts no longer match their SRI hashes, and a
            # <meta> CSP could carry upgrade-insecure-requests.
            body = INTEGRITY_ATTR.sub(b"", body)
            body = META_CSP.sub(b"", body)
        return body

    def _upstream_url(self, request: web.Request) -> URL:
        return URL(self.upstream_origin + request.raw_path, encoded=True)

    def _upstream_headers(self, request: web.Request, skip: frozenset) -> CIMultiDict:
        local = self._local_origin(request)
        drop = skip | _connection_tokens(request.headers)
        headers: CIMultiDict = CIMultiDict()
        for key, value in request.headers.items():
            lower = key.lower()
            if lower in drop:
                continue
            if lower in ("origin", "referer"):
                value = self._to_upstream(value, local)
            headers.add(key, value)
        if self.forward_headers:
            remote = request.remote or ""
            prior = request.headers.get("X-Forwarded-For")
            headers["X-Forwarded-For"] = f"{prior}, {remote}" if prior else remote
            headers["X-Real-IP"] = remote
            headers["X-Forwarded-Proto"] = request.scheme
        for key, value in self.extra_headers.items():
            headers[key] = value
        return headers

    def _response_headers(self, upstream: aiohttp.ClientResponse, local: str) -> CIMultiDict:
        drop = RESPONSE_SKIP | _connection_tokens(upstream.headers)
        headers: CIMultiDict = CIMultiDict()
        for key, value in upstream.headers.items():
            lower = key.lower()
            if lower in drop:
                continue
            if lower == "set-cookie":
                value = rewrite_set_cookie(value)
            elif lower in URL_HEADERS:
                value = self.rewrite_text(value, local)
            headers.add(key, value)
        return headers

    # --------------------------------------------------------------- handlers

    async def handle(self, request: web.Request) -> web.StreamResponse:
        started = time.perf_counter()
        if self.start_path and request.method == "GET" and request.raw_path == "/":
            response: web.StreamResponse = web.Response(status=302, headers={"Location": self.start_path})
        elif _is_websocket(request):
            response = await self._proxy_websocket(request)
        else:
            response = await self._proxy_http(request)
        elapsed = (time.perf_counter() - started) * 1000
        log.info("%s %s -> %s (%.0f ms)", request.method, request.raw_path, response.status, elapsed)
        return response

    def _bad_gateway(self, exc: BaseException) -> web.Response:
        log.warning("upstream error: %r", exc)
        return web.Response(
            status=502,
            text=f"localportal: could not reach {self.upstream_origin}\n{exc!r}\n",
        )

    async def _proxy_http(self, request: web.Request) -> web.StreamResponse:
        headers = self._upstream_headers(request, REQUEST_SKIP)
        headers["Accept-Encoding"] = ACCEPT_ENCODING
        body = await request.read() if request.body_exists else None
        try:
            upstream = await self._session.request(
                request.method,
                self._upstream_url(request),
                headers=headers,
                data=body,
                allow_redirects=False,
                # Mirror the browser exactly: don't invent headers it didn't send.
                skip_auto_headers=("User-Agent", "Content-Type"),
            )
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            return self._bad_gateway(exc)
        async with upstream:
            return await self._relay(request, upstream)

    async def _relay(self, request: web.Request, upstream: aiohttp.ClientResponse) -> web.StreamResponse:
        local = self._local_origin(request)
        headers = self._response_headers(upstream, local)
        status, reason = upstream.status, upstream.reason
        encoding = upstream.headers.get("Content-Encoding", "identity").strip().lower()
        length = upstream.headers.get("Content-Length", "")
        exact_length = int(length) if length.isdigit() and encoding == "identity" else None

        if request.method == "HEAD" or status in (204, 304) or status < 200:
            response = web.Response(status=status, reason=reason, headers=headers)
            if request.method == "HEAD" and exact_length is not None:
                response.headers["Content-Length"] = str(exact_length)
            return response

        mime = _mime_type(upstream.headers.get("Content-Type", ""))
        if self.rewrite and _is_rewritable(mime):
            try:
                body = await upstream.read()
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                return self._bad_gateway(exc)
            body = self._rewrite_body(body, local, mime)
            return web.Response(status=status, reason=reason, headers=headers, body=body)

        # Everything else (images, video, downloads, event streams) is streamed.
        response = web.StreamResponse(status=status, reason=reason, headers=headers)
        if exact_length is not None:
            response.content_length = exact_length
        await response.prepare(request)
        try:
            async for chunk in upstream.content.iter_any():
                await response.write(chunk)
            await response.write_eof()
        except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionError) as exc:
            log.debug("stream for %s ended early: %r", request.raw_path, exc)
        return response

    async def _proxy_websocket(self, request: web.Request) -> web.StreamResponse:
        headers = self._upstream_headers(request, WS_SKIP)
        protocols = [
            p.strip() for p in request.headers.get("Sec-WebSocket-Protocol", "").split(",") if p.strip()
        ]
        url = self._upstream_url(request)
        url = url.with_scheme("wss" if url.scheme == "https" else "ws")
        try:
            upstream_ws = await self._session.ws_connect(
                url, headers=headers, protocols=protocols, max_msg_size=0
            )
        except aiohttp.WSServerHandshakeError as exc:
            return web.Response(status=exc.status or 502, text=f"localportal: websocket refused: {exc.message}\n")
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            return self._bad_gateway(exc)

        chosen = (upstream_ws.protocol,) if upstream_ws.protocol else ()
        local_ws = web.WebSocketResponse(protocols=chosen, max_msg_size=0)
        await local_ws.prepare(request)

        async def pipe(src, dst) -> None:
            try:
                async for msg in src:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        await dst.send_str(msg.data)
                    elif msg.type == aiohttp.WSMsgType.BINARY:
                        await dst.send_bytes(msg.data)
                    elif msg.type == aiohttp.WSMsgType.ERROR:
                        break
            except Exception as exc:  # noqa: BLE001 - one side hung up mid-send
                log.debug("websocket pipe closed: %r", exc)
            if not dst.closed:
                await dst.close(code=_ws_close_code(src.close_code))

        tasks = [
            asyncio.ensure_future(pipe(local_ws, upstream_ws)),
            asyncio.ensure_future(pipe(upstream_ws, local_ws)),
        ]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await upstream_ws.close()
            await local_ws.close()
        return local_ws

    # ---------------------------------------------------------------- running

    @property
    def local_url(self) -> str:
        host = self.host
        if host in ("0.0.0.0", "", "::"):
            host = "127.0.0.1"
        elif ":" in host:
            host = f"[{host}]"
        return f"http://{host}:{self.port}"

    async def serve(self, open_browser: bool = False) -> None:
        """Run until cancelled."""
        runner = web.AppRunner(
            self.make_app(),
            access_log=None,
            max_line_size=MAX_HEADER_SIZE,
            max_field_size=MAX_HEADER_SIZE,
        )
        await runner.setup()
        try:
            site = web.TCPSite(runner, self.host, self.port)
            await site.start()
            print()
            print(f"  localportal is mirroring {self.upstream_origin}")
            print(f"  Open  {self.local_url}{self.start_path or '/'}")
            print("  Press Ctrl+C to stop.")
            print(flush=True)
            if open_browser:
                webbrowser.open(self.local_url + (self.start_path or "/"))
            await asyncio.Event().wait()
        finally:
            await runner.cleanup()

    def run(self, open_browser: bool = False) -> None:
        """Blocking entry point; returns after Ctrl+C."""
        try:
            asyncio.run(self.serve(open_browser=open_browser))
        except KeyboardInterrupt:
            pass

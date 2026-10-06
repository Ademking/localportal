import asyncio

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from localportal import LocalPortal
from localportal.proxy import rewrite_set_cookie


def make_upstream() -> web.Application:
    async def page(request):
        origin = f"http://{request.host}"
        html = (
            f'<a href="{origin}/next">next</a>'
            f'<script src="//{request.host}/app.js" integrity="sha384-abc"></script>'
            f'<a href="https://other.example/x">other</a>'
            f'<script>var u = "{origin.replace("/", chr(92) + "/")}/api";</script>'
        )
        resp = web.Response(text=html, content_type="text/html")
        resp.headers["Strict-Transport-Security"] = "max-age=31536000"
        resp.headers["Content-Security-Policy"] = "upgrade-insecure-requests"
        resp.set_cookie("sid", "1", domain=request.host.split(":")[0], secure=True, samesite="None")
        return resp

    async def redirect(request):
        return web.Response(status=302, headers={"Location": f"http://{request.host}/landing?a=1"})

    async def echo(request):
        body = await request.read()
        return web.json_response(
            {
                "host": request.host,
                "method": request.method,
                "path": request.raw_path,
                # Without "//" so the mirror's JSON rewriting leaves it alone.
                "origin": request.headers.get("Origin", "").replace("//", ""),
                "body": body.decode(),
            }
        )

    async def binary(request):
        return web.Response(body=bytes(range(256)) * 100, content_type="application/octet-stream")

    async def ws(request):
        sock = web.WebSocketResponse(protocols=("chat",))
        await sock.prepare(request)
        async for msg in sock:
            if msg.type == aiohttp.WSMsgType.TEXT:
                await sock.send_str("echo:" + msg.data)
            elif msg.type == aiohttp.WSMsgType.BINARY:
                await sock.send_bytes(msg.data[::-1])
        return sock

    app = web.Application()
    app.router.add_get("/", page)
    app.router.add_get("/redirect", redirect)
    app.router.add_route("*", "/echo", echo)
    app.router.add_get("/bin", binary)
    app.router.add_get("/ws", ws)
    return app


def run(scenario):
    async def main():
        upstream = TestServer(make_upstream())
        await upstream.start_server()
        target = f"http://{upstream.host}:{upstream.port}"
        portal = LocalPortal(target, resolve=False)
        client = TestClient(TestServer(portal.make_app()))
        await client.start_server()
        try:
            local = f"http://{client.server.host}:{client.server.port}"
            await scenario(client, local, target)
        finally:
            await client.close()
            await upstream.close()

    asyncio.run(main())


def test_html_is_rewritten_to_the_mirror():
    async def scenario(client, local, target):
        resp = await client.get("/")
        assert resp.status == 200
        html = await resp.text()
        local_host = local.split("://")[1]
        assert f'href="{local}/next"' in html
        assert f'src="//{local_host}/app.js"' in html
        assert "integrity" not in html
        assert "https://other.example/x" in html
        assert local.replace("/", "\\/") + "/api" in html
        assert target not in html
        assert "Strict-Transport-Security" not in resp.headers
        assert "Content-Security-Policy" not in resp.headers
        cookie = resp.headers["Set-Cookie"]
        assert "Domain" not in cookie and "Secure" not in cookie
        assert "SameSite=Lax" in cookie

    run(scenario)


def test_redirects_stay_on_the_mirror():
    async def scenario(client, local, target):
        resp = await client.get("/redirect", allow_redirects=False)
        assert resp.status == 302
        assert resp.headers["Location"] == f"{local}/landing?a=1"

    run(scenario)


def test_methods_bodies_and_headers_are_forwarded():
    async def scenario(client, local, target):
        resp = await client.post("/echo?x=1&y=%20", data=b"hello", headers={"Origin": local})
        data = await resp.json()
        assert data["host"] == target.split("://")[1]
        assert data["method"] == "POST"
        assert data["path"] == "/echo?x=1&y=%20"
        assert data["origin"] == target.replace("//", "")
        assert data["body"] == "hello"

    run(scenario)


def test_binary_is_streamed_unchanged():
    async def scenario(client, local, target):
        resp = await client.get("/bin")
        assert await resp.read() == bytes(range(256)) * 100
        assert resp.headers["Content-Length"] == str(25600)

    run(scenario)


def test_websockets_are_proxied():
    async def scenario(client, local, target):
        sock = await client.ws_connect("/ws", protocols=("chat",))
        assert sock.protocol == "chat"
        await sock.send_str("hi")
        assert (await sock.receive()).data == "echo:hi"
        await sock.send_bytes(b"abc")
        assert (await sock.receive()).data == b"cba"
        await sock.close()

    run(scenario)


def test_cookie_rewrite_keeps_prefixed_cookies_secure():
    assert rewrite_set_cookie("__Host-id=1; Path=/; Secure; SameSite=None") == (
        "__Host-id=1; Path=/; Secure; SameSite=None"
    )
    assert rewrite_set_cookie("a=1; Domain=.example.com; Path=/; Secure; HttpOnly; Partitioned") == (
        "a=1; Path=/; HttpOnly"
    )


def test_rewrite_ignores_lookalike_hosts():
    portal = LocalPortal("https://example.com", resolve=False)
    local = "http://127.0.0.1:3000"
    src = (
        b"https://example.com/a https://www.example.com/b //example.com/c "
        b"https://example.com.evil.net/d https://example.com:8443/e https://notexample.com/f "
        b"https://cdn.example.com/g https://example.com:443/h"
    )
    out = portal.rewrite_bytes(src, local)
    assert out == (
        b"http://127.0.0.1:3000/a http://127.0.0.1:3000/b //127.0.0.1:3000/c "
        b"https://example.com.evil.net/d https://example.com:8443/e https://notexample.com/f "
        b"https://cdn.example.com/g http://127.0.0.1:3000/h"
    )

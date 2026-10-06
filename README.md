<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/Ademking/localportal/main/assets/logo-dark.svg">
    <img src="https://raw.githubusercontent.com/Ademking/localportal/main/assets/logo-light.svg" alt="localportal" width="440">
  </picture>
</p>

<p align="center">
  <b>Mirror any website on <code>127.0.0.1</code>, with one command.</b>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.9%2B-7C3AED" alt="Python 3.9+">
  <img src="https://img.shields.io/badge/license-MIT-7C3AED" alt="MIT license">
  <img src="https://img.shields.io/badge/dependency-aiohttp-7C3AED" alt="Only dependency: aiohttp">
</p>

---

```bash
localportal -u https://example.com
```

Open **http://127.0.0.1:3000** and the browser shows example.com, with working links, logins, forms, images, video and websockets. The site is served through your machine, as if it were running locally.

It does the same job as an nginx `proxy_pass` block, without installing nginx or writing any config. It also handles the details a plain proxy gets wrong, such as redirects, cookies, absolute links and HTTPS-only headers, so you stay on the mirror instead of being sent back to the real site.

## Install

```bash
pip install localportal
```

To install the latest version straight from GitHub:

```bash
pip install git+https://github.com/Ademking/localportal
```

## Quick start

```bash
# Mirror a site on http://127.0.0.1:3000
localportal -u https://example.com

# Choose a port and open the browser automatically
localportal -u https://news.ycombinator.com -p 8080 --open

# The scheme is optional (https is assumed)
localportal python.org
```

Press `Ctrl+C` to stop. Every request is logged as it happens:

```
  localportal is mirroring https://www.python.org
  Open  http://127.0.0.1:3000/
  Press Ctrl+C to stop.

02:23:42  GET / -> 200 (330 ms)
02:23:42  GET /static/stylesheets/mq.css -> 200 (310 ms)
02:23:42  GET /static/img/python-logo.png -> 200 (313 ms)
```

## What it's good for

- **Local development against a real site.** Work on a frontend or browser extension while a staging or production site appears to run on localhost.
- **Testing and automation.** Point Playwright, Selenium, Lighthouse or your scripts at a stable local address.
- **Debugging.** Every request passes through one place you control, so you can see exactly what the browser asks for.
- **Sharing on your network.** Run with `--bind 0.0.0.0` and other devices on your LAN can open the mirror.
- **Demos.** Show a site on a local address, with extra headers (such as an auth token) added to every request.

## Options

| Option | Description |
| --- | --- |
| `-u, --url URL` | Website to mirror. The scheme is optional. |
| `-p, --port PORT` | Local port. Default: `3000`. |
| `-b, --bind ADDR` | Address to listen on. Default: `127.0.0.1`. Use `0.0.0.0` to share on your LAN. |
| `-H, --header 'NAME: VALUE'` | Extra header to send upstream. Repeatable. |
| `--no-rewrite` | Leave absolute links in response bodies untouched. |
| `--no-resolve` | Don't follow the startup `www`/`https` redirect. |
| `--forward-headers` | Send `X-Forwarded-For`, `X-Real-IP` and `X-Forwarded-Proto` upstream. |
| `-k, --insecure` | Don't verify the upstream TLS certificate (for self-signed staging servers). |
| `-o, --open` | Open the mirror in your browser. |
| `-q, --quiet` | Don't log each request. |
| `-V, --version` | Print the version. |

More examples:

```bash
# Send an auth header with every request
localportal -u https://staging.myapp.com -H "Authorization: Bearer <token>"

# Start on a specific page: http://127.0.0.1:3000/ redirects to /docs
localportal -u https://example.com/docs

# Mirror a dev server with a self-signed certificate
localportal -u https://192.168.1.20:8443 -k

# Same as the localportal command
python -m localportal -u https://example.com
```

## How it works

```
 browser                     localportal                         real site
 http://127.0.0.1:3000  ──▶  rewrite Host, Origin, Referer  ──▶  https://example.com
                        ◀──  rewrite links, cookies, headers ◀──
```

Each request is replayed against the real site with the site's own `Host` header and TLS SNI. Then the response is adjusted so the browser keeps treating the mirror as the site:

| Without localportal | What localportal does |
| --- | --- |
| Redirects send you to the real site | `Location`, `Content-Location`, `Link` and `Refresh` headers are rewritten to point at the mirror |
| Absolute links (`https://site/…`, `//site/…`, `https:\/\/site` in JSON) leave the mirror | They are rewritten in HTML, CSS, JS, JSON, XML and SVG |
| Cookies with `Domain=site` and `Secure` are rejected on plain http | Those attributes are removed so logins keep working (`__Host-` and `__Secure-` cookies keep `Secure`) |
| HSTS, CSP `upgrade-insecure-requests` and Alt-Svc force HTTPS | These headers are dropped, along with `<meta>` CSP tags and the SRI `integrity` attributes that rewriting would invalidate |
| The server's CSRF checks reject the wrong `Origin` or `Referer` | Both are rewritten back to the real site on the way out |
| `example.com` redirects to `www.example.com`, creating a redirect loop | The canonical host is detected once at startup |
| WebSockets don't pass through | They are proxied in both directions, with subprotocols |
| Large files get buffered | Video, downloads and Server-Sent Events are streamed, and Range requests work |

### Compared with nginx

This nginx config:

```nginx
server {
    listen 3000;
    location / {
        proxy_pass https://example.com;
        proxy_set_header Host example.com;
        proxy_ssl_server_name on;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
    }
}
```

is equivalent to:

```bash
localportal -u https://example.com
```

localportal also does everything in the table above, which nginx would need `sub_filter`, `proxy_redirect`, `proxy_cookie_domain` and WebSocket upgrade blocks to do.

## Use it from Python

```python
from localportal import LocalPortal

LocalPortal("https://example.com", port=3000).run()
```

All CLI options are available as keyword arguments:

```python
LocalPortal(
    "https://staging.myapp.com",
    host="0.0.0.0",
    port=8080,
    extra_headers={"Authorization": "Bearer <token>"},
    verify_ssl=False,
).run(open_browser=True)
```

To embed it in your own aiohttp server or tests, `LocalPortal(...).make_app()` returns the `aiohttp.web.Application`, and `await LocalPortal(...).serve()` runs it inside an existing event loop.

## Limitations

- **Only the main host is mirrored**, plus its `www.` version. Subdomains such as `cdn.example.com` and `api.example.com`, and third-party hosts, load directly from the internet. That's fine for public assets, but cross-origin APIs that check `Origin` may refuse requests from the mirror.
- **URLs built at runtime** by JavaScript (`"https://" + host`) can't be rewritten ahead of time.
- **Third-party sign-in** (Google, GitHub, etc.) usually fails, because the provider redirects back to the real domain.
- **One origin per port.** Every site you mirror on the same port shares cookies, storage and service workers. Use a different port for each site, or clear the site data when you switch.
- **Bot protection** (Cloudflare challenges, CAPTCHAs) sees ordinary traffic from your IP and may still challenge you.

Only mirror sites you're allowed to access, and respect their terms of service.

## Development

```bash
git clone https://github.com/Ademking/localportal
cd localportal
pip install -e ".[dev]"
pytest
```

The tests start a fake upstream server and check HTML rewriting, redirects, cookies, request forwarding, binary streaming and WebSockets.

## License

[MIT](LICENSE)

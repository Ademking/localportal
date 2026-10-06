"""Command line interface: ``localportal -u https://example.com``."""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Optional, Sequence

from . import __version__
from .proxy import LocalPortal


def _parse_header(raw: str) -> tuple:
    name, sep, value = raw.partition(":")
    if not sep or not name.strip():
        raise argparse.ArgumentTypeError(f"expected 'Name: value', got {raw!r}")
    return name.strip(), value.strip()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="localportal",
        description="Mirror any website on 127.0.0.1 through a local reverse proxy.",
        epilog="example: localportal -u https://example.com   then open http://127.0.0.1:3000",
    )
    parser.add_argument("target", nargs="?", help=argparse.SUPPRESS)
    parser.add_argument("-u", "--url", help="website to mirror, e.g. https://example.com")
    parser.add_argument("-p", "--port", type=int, default=3000, help="local port (default: 3000)")
    parser.add_argument(
        "-b",
        "--bind",
        default="127.0.0.1",
        metavar="ADDR",
        help="address to listen on (default: 127.0.0.1; use 0.0.0.0 to share on your LAN)",
    )
    parser.add_argument(
        "-H",
        "--header",
        action="append",
        default=[],
        type=_parse_header,
        metavar="'NAME: VALUE'",
        help="extra header to send upstream (repeatable)",
    )
    parser.add_argument(
        "--no-rewrite",
        action="store_true",
        help="leave absolute links in HTML/CSS/JS untouched",
    )
    parser.add_argument(
        "--no-resolve",
        action="store_true",
        help="don't follow the site's startup redirect (e.g. example.com -> www.example.com)",
    )
    parser.add_argument(
        "--forward-headers",
        action="store_true",
        help="send X-Forwarded-For, X-Real-IP and X-Forwarded-Proto upstream",
    )
    parser.add_argument(
        "-k", "--insecure", action="store_true", help="don't verify the upstream TLS certificate"
    )
    parser.add_argument("-o", "--open", action="store_true", help="open the mirror in your browser")
    parser.add_argument("-q", "--quiet", action="store_true", help="don't log each request")
    parser.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    url = args.url or args.target
    if not url:
        parser.error("a website is required, e.g. localportal -u https://example.com")

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s  %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        portal = LocalPortal(
            url,
            host=args.bind,
            port=args.port,
            rewrite=not args.no_rewrite,
            resolve=not args.no_resolve,
            verify_ssl=not args.insecure,
            forward_headers=args.forward_headers,
            extra_headers=dict(args.header),
        )
    except ValueError as exc:
        parser.error(str(exc))

    try:
        portal.run(open_browser=args.open)
    except OSError as exc:
        print(f"localportal: cannot listen on {args.bind}:{args.port}: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

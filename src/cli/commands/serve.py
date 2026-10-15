"""``engram serve`` — run the remote-mode HTTP server (ADR-0009 Phase 1)."""

from __future__ import annotations


def add_parser(sub) -> None:
    from ...serve import (
        ALLOW_NONLOCAL_ENV,
        AUTH_ENV,
        DEFAULT_HOST,
        DEFAULT_PORT,
        HOST_ENV,
        PORT_ENV,
    )

    p = sub.add_parser(
        "serve",
        help="Run the remote-mode JSON/HTTP server on 127.0.0.1 (publish it with `tailscale serve`)",
    )
    p.add_argument("--host", default=None, help=f"Bind address (default {DEFAULT_HOST}; env {HOST_ENV})")
    p.add_argument("--port", type=int, default=None, help=f"Port (default {DEFAULT_PORT}; env {PORT_ENV})")
    p.add_argument(
        "--auth",
        choices=("caps", "none"),
        default=None,
        help=f"caps = require a Tailscale-App-Capabilities grant (default); none = trust every caller (tests only). Env {AUTH_ENV}",
    )
    p.add_argument(
        "--allow-nonlocal",
        action="store_true",
        help=f"Permit binding a non-loopback address (normally refused; env {ALLOW_NONLOCAL_ENV}=1)",
    )
    p.set_defaults(func=cmd_serve)


def cmd_serve(args) -> None:
    import logging

    from ...serve import serve

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    serve(args.host, args.port, auth=args.auth, allow_nonlocal=True if args.allow_nonlocal else None)

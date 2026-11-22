"""Uvicorn entry point exposing its graceful-exit handle to loopback lifecycle API."""

from __future__ import annotations

import os
import socket

import uvicorn

from backend.app import DEMO_EXECUTION, app


def main() -> None:
    host = os.getenv("BTC_LAB_API_HOST", "127.0.0.1")
    port = int(os.getenv("BTC_LAB_API_PORT", "8011"))
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, proxy_headers=not DEMO_EXECUTION))
    app.state.uvicorn_server = server
    if DEMO_EXECUTION:
        # A serving socket must be reserved before Demo startup can submit orders.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            if os.name == "nt":
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            listener.bind((host, port))
            app.state.demo_listener_reserved = True
            try:
                server.run(sockets=[listener])
            finally:
                app.state.demo_listener_reserved = False
    else:
        server.run()


if __name__ == "__main__":
    main()

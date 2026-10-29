"""Run one single-tenant relay worker with bounded, private transports."""

import argparse


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="acp-bridge",
        description="Serve the authenticated ACP WebSocket to stdio relay (one worker).",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8080, help="TCP port (default: 8080)")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")

    # Parse help before importing the server or reading any credentials.
    import uvicorn

    from .app import create_app, silence_transport_logs
    from .config import Settings

    try:
        settings = Settings.from_env()
    except RuntimeError:
        parser.exit(2, "acp-bridge: Relay configuration is invalid\n")

    silence_transport_logs()
    uvicorn.run(
        create_app(settings),
        host=args.host,
        port=args.port,
        workers=1,
        lifespan="on",
        access_log=False,
        log_level="info",
        ws="websockets-sansio",
        ws_max_size=settings.max_frame_bytes,
        ws_max_queue=4,
        ws_per_message_deflate=False,
    )


if __name__ == "__main__":
    main()

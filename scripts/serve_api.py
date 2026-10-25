"""API launcher with a psycopg-compatible Selector loop on Windows."""

import argparse
import asyncio
import selectors
import sys

import uvicorn


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    server = uvicorn.Server(uvicorn.Config("apps.api.main:app", host=args.host, port=args.port))
    factory = (
        (lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()))
        if sys.platform == "win32"
        else None
    )
    asyncio.run(server.serve(), loop_factory=factory)


if __name__ == "__main__":
    main()

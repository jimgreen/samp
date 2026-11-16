import argparse

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="Run SAMP web service")
    parser.add_argument("--db", default="data/samp.db", help="SQLite database path")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    from .app import create_app

    uvicorn.run(create_app(args.db), host=args.host, port=args.port)


if __name__ == "__main__":
    main()


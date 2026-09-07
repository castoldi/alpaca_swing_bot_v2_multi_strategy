"""Manager-owned dashboard entry point with a lifetime singleton lock."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import runtime


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8004)
    args = parser.parse_args()
    try:
        with runtime.tracked("dashboard", {"port": args.port}):
            import uvicorn
            uvicorn.run("dashboard.server:app", host="0.0.0.0", port=args.port)
    except runtime.AlreadyRunning as exc:
        print(exc)


if __name__ == "__main__":
    main()

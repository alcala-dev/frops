"""Allow `python -m frops` invocation."""

from frops.cli import main

if __name__ == "__main__":
    raise SystemExit(main())

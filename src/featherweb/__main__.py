"""Allow ``python -m featherweb`` to reach the command line."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())

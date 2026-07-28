"""``python -m housekeeper``.

The ``__main__`` guard is load-bearing, not decoration: parser workers start with ``forkserver``,
whose children re-import ``__main__`` (it inherits ``spawn``'s semantics). Without the guard every
worker would re-run the whole command.
"""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())

"""``python -m housekeeper``.

``__main__`` guard is load-bearing: forkserver workers re-import ``__main__``; without the
guard every worker would re-run the whole command.
"""

from .cli import main


if __name__ == "__main__":
    raise SystemExit(main())

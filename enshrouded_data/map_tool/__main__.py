import sys

if sys.version_info < (3,13):
    print('Enshrouded Maps requires Python 3.13 or later. Install Python 3.13 and run the launcher again.',file=sys.stderr)
    raise SystemExit(2)

from .cli import main

raise SystemExit(main())

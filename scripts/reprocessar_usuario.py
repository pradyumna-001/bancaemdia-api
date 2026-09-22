"""Plan or enqueue tenant-scoped rereading and house-collection reprocessing.

Use ``usuario --usuario-id ID --versao-prompt VERSION --sim`` to reread stored
media; omitting ``--sim`` is a read-only cost estimate.
"""

import sys

from bancaemdia.cli.reprocess import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

"""Admin chores from the command line.

    docker compose exec tattle python -m tattle.admin reset-password

reset-password forgets the admin password chosen in the browser. After a
restart the dashboard asks for a new one, with a new setup code (in
data/setup-code.txt and the log). A TATTLE_ADMIN_PASSWORD in .env is not
affected: change it there.
"""

from __future__ import annotations

import sys

from . import db
from .config import load_settings

ADMIN_HASH_SETTING = "admin_password_hash"  # the same key api.py stores the hash under


def main(argv: list[str]) -> int:
    if argv != ["reset-password"]:
        print(__doc__, file=sys.stderr)
        return 2
    settings = load_settings()
    conn = db.connect(settings.db_path)
    try:
        conn.execute("DELETE FROM settings WHERE key = ?", (ADMIN_HASH_SETTING,))
    finally:
        conn.close()
    print("The admin password is forgotten. Now run: docker compose restart tattle")
    print("Then open the dashboard and use the new setup code from: docker compose logs tattle")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

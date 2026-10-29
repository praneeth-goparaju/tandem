from __future__ import annotations

from src.brain.media.store import delete_expired
from src.brain.runtime.edit_backups import delete_old

EXPIRY_DAYS = 30


def main() -> None:
    print(delete_expired(EXPIRY_DAYS))
    delete_old(EXPIRY_DAYS)


if __name__ == "__main__":
    main()

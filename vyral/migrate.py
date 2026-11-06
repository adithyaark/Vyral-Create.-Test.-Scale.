"""Set up hosted mode's database tables and storage with `python -m vyral.migrate`."""
from dotenv import load_dotenv

from .tools import ROOT


def main() -> None:
    load_dotenv(ROOT / ".env", override=False)
    from . import accounts, blobs, db

    print("Applied:", ", ".join(db.migrate()))
    blobs.ensure_bucket(accounts.limits()["upload_mb"])
    print(f"Private storage bucket ready: {blobs.BUCKET}")


if __name__ == "__main__":
    main()

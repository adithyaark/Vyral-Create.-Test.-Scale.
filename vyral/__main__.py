"""Start Vyral with `python -m vyral` and open it in the browser."""
import argparse
import threading
import webbrowser

import uvicorn
from dotenv import load_dotenv

from .tools import ROOT, is_hosted


def main() -> None:
    parser = argparse.ArgumentParser(description="Vyral: Create. Test. Scale.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    # Real environment variables win, so a server's settings aren't overridden by a stray file.
    load_dotenv(ROOT / ".env", override=False)
    # Hosted mode uses localhost, so the browser's sign-in matches links in Supabase emails.
    host = "localhost" if is_hosted() else "127.0.0.1"
    url = f"http://{host}:{args.port}"
    if not args.no_browser:
        threading.Timer(1.5, webbrowser.open, [url]).start()
    print(f"Vyral is running at {url}. Press Ctrl+C to stop.")
    uvicorn.run("vyral.app:app", host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()

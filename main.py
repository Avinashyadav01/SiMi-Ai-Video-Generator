"""Start the SiMi browser application."""
from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")


def main() -> int:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    parser=argparse.ArgumentParser(description="SiMi AI Content Automation")
    parser.add_argument("--host",default=os.getenv("HOST","127.0.0.1"),help="Bind address (use 0.0.0.0 only behind a trusted production proxy)")
    parser.add_argument("--port",type=int,default=int(os.getenv("PORT","8000")))
    parser.add_argument("--reload",action="store_true",help="Enable development auto-reload")
    args=parser.parse_args()
    import uvicorn
    uvicorn.run("app:app",host=args.host,port=args.port,reload=args.reload,workers=1,proxy_headers=False)
    return 0


if __name__=="__main__":
    raise SystemExit(main())

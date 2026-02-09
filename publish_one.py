import argparse
import os
from pathlib import Path

from publish_docs import load_cfg, DocsPublisher


def main(md_path: str, pass_no: int, cfg_path: str) -> None:
    token = os.getenv("CONF_TOKEN")
    if not token:
        raise SystemExit("Set CONF_TOKEN env var")

    cfg = load_cfg(cfg_path)
    pub = DocsPublisher(cfg, token)
    p = Path(md_path)
    if not p.exists():
        raise SystemExit(f"File not found: {md_path}")
    pub.publish_file(p, pass_no)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("md_path")
    ap.add_argument("--pass", dest="pass_no", type=int, choices=[1, 2], default=1)
    ap.add_argument("--cfg", default="publish.yml")
    args = ap.parse_args()

    main(args.md_path, args.pass_no, args.cfg)

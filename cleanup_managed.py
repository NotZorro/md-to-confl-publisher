import argparse
import os

from confl_client import Confluence
from publish_docs import load_cfg, managed_label_from_cfg


def main(cfg_path: str, delete: bool, list_only: bool) -> None:
    token = os.getenv("CONF_TOKEN")
    if not token:
        raise SystemExit("Set CONF_TOKEN env var")

    cfg = load_cfg(cfg_path)
    conf = Confluence(cfg.base_url, token)

    managed_label = managed_label_from_cfg(cfg)
    cql = f'ancestor={cfg.docs_root_id} and type=page and label="{managed_label}"'

    pages = list(conf.cql_iter(cql, expand="ancestors"))
    print(f"Found {len(pages)} managed pages under root {cfg.docs_root_id}")

    for p in pages:
        print(f"{p['id']}\t{p.get('title')}")

    if delete and not list_only:
        for p in pages:
            conf.delete_page(p['id'])
        print("Deleted.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--cfg", default="publish.yml")
    ap.add_argument("--delete", action="store_true")
    ap.add_argument("--list-only", action="store_true")
    args = ap.parse_args()

    main(args.cfg, delete=args.delete, list_only=args.list_only)

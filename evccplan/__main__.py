"""Entry point: `python -m evccplan serve|once [--config PATH]`."""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from pathlib import Path
import threading
import time

from . import config as cfgmod
from .clients import EvccClient, HAClient, OrsClient
from .runner import Runner
from .store import Store
from .web import make_server


def build(cfg) -> Runner:
    return Runner(cfg, Store(cfg.db_path), HAClient(cfg.ha_url, cfg.ha_token),
                  EvccClient(cfg.evcc_url, cfg.evcc_key), OrsClient(cfg.ors_key) if cfg.ors_key else None)


def ensure_config(path: str) -> None:
    """First start: create the config file from the bundled example if it does not exist yet."""
    target = Path(path)
    example = Path(__file__).resolve().parent.parent / "config.example.yaml"
    if target.exists() or not example.is_file():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(example, target)
    logging.warning("No configuration found, copied the example to %s. Please adjust it.", target)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="evccplan")
    ap.add_argument("command", choices=["serve", "once"])
    ap.add_argument("--config", default="/data/config.yaml")
    ap.add_argument("--dry-run", action="store_true", help="once: write nothing, regardless of the config")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ensure_config(args.config)
    cfg = cfgmod.load(args.config)
    missing = cfgmod.problems(cfg)
    if missing:
        for m in missing:
            logging.error("Configuration: %s", m)
        return 2
    runner = build(cfg)
    if args.command == "once":
        print(json.dumps(runner.run(force_dry=args.dry_run), ensure_ascii=False, indent=2))
        return 0
    logging.info("Start, mode: %s", "dry run" if cfg.dry_run else "live")
    threading.Thread(target=make_server(runner, cfg).serve_forever, daemon=True).start()
    while True:
        runner.run()
        time.sleep(cfg.run_every_min * 60)


if __name__ == "__main__":
    sys.exit(main())

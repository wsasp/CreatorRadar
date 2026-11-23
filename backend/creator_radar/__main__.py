import argparse
import json
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import uvicorn
from filelock import Timeout
from sqlalchemy import select

from . import discovery
from .api import create_app
from .asr_setup import prepare_asr
from .config import Config
from .crawler_setup import install_crawler
from .db import make_engine, migrate, write_transaction
from .queue import Submission, submit, submit_in
from .schema import PLATFORMS, topics, works
from .worker import run


def main():
    parser = argparse.ArgumentParser(prog="python -m creator_radar")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("migrate", "api", "worker"):
        commands.add_parser(command)
    workspace_path = commands.add_parser("workspace-path")
    workspace_path.add_argument("--json", action="store_true")
    crawler = commands.add_parser("setup-crawler")
    crawler.add_argument("--runtime", type=Path)
    crawler.add_argument("--startup", action="store_true")
    commands.add_parser("setup-asr")
    fixture = commands.add_parser("enqueue-test")
    fixture.add_argument("--scenario", choices=("success", "fail_once", "waiting_login", "batch"),
                         default="success")
    fixture.add_argument("--seconds", type=float, default=3)
    fixture.add_argument("--key")
    fixture.add_argument("--platform", choices=PLATFORMS, default="bilibili")
    media = commands.add_parser("fetch-media", help="Queue media acquisition for a video topic")
    media.add_argument("topic_id")
    args = parser.parse_args()
    config = Config()

    if args.command == "migrate":
        migrate(config)
    elif args.command == "workspace-path":
        path = str(config.workspace_dir)
        print(json.dumps({"path": path}) if args.json else path)
    elif args.command == "setup-crawler":
        try:
            install_crawler(config, args.runtime, startup=args.startup)
        except (OSError, ValueError, subprocess.CalledProcessError, Timeout) as error:
            print(f"MediaCrawler setup failed: {error}", file=sys.stderr)
            raise SystemExit(1) from None
    elif args.command == "setup-asr":
        try:
            prepare_asr(config)
        except (OSError, ValueError, subprocess.CalledProcessError, Timeout) as error:
            print(f"ASR setup failed: {error}", file=sys.stderr)
            raise SystemExit(1) from None
    elif args.command == "api":
        uvicorn.run(
            create_app(config), host="127.0.0.1", port=config.port,
            proxy_headers=False, access_log=False,
        )
    elif args.command == "worker":
        run(make_engine(config), config)
    elif args.command == "fetch-media":
        with write_transaction(make_engine(config)) as connection:
            work = connection.execute(select(works).join(topics).where(
                topics.c.id == args.topic_id, ~topics.c.withdrawn, works.c.kind == "video",
            )).mappings().first()
            if work is None:
                parser.error("fetch-media requires a video topic ID")
            if work["platform"] not in discovery.read_settings(connection)["enabled_platforms"]:
                parser.error("fetch-media requires an enabled platform")
            job_id = submit_in(connection, Submission(
                "media_fetch", work["id"], work["title"], work["platform"],
                {"work_id": work["id"], "topic_id": args.topic_id, "trigger": "manual"},
            ))
        print(json.dumps({"job_id": job_id}))
    else:
        if config.workspace != "acceptance":
            parser.error("enqueue-test requires CREATOR_RADAR_WORKSPACE=acceptance")
        if not 0.1 <= args.seconds <= 300:
            parser.error("--seconds must be between 0.1 and 300")
        key = args.key or uuid4().hex
        if args.scenario == "batch":
            job = Submission("search", key, "多平台队列验收")
            children = tuple(
                test_job(key, platform, scenario, args.seconds, kind="search")
                for platform, scenario in zip(
                    PLATFORMS, ("success", "fail_once", "waiting_login"), strict=True,
                )
            )
        else:
            job = test_job(key, args.platform, args.scenario, args.seconds)
            children = ()
        engine = make_engine(config)
        print(json.dumps({"job_id": submit(engine, job, children)}))


def test_job(key, platform, scenario, seconds, *, kind="self_test"):
    return Submission(
        kind, key, "队列验收任务", platform,
        {"handler": "acceptance", "seconds": seconds, "scenario": scenario},
    )


if __name__ == "__main__":
    main()

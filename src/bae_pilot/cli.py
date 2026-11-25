"""Local management commands. Demo reads only database connectivity, never model configuration."""

import argparse
import asyncio
import json
import sys
import tomllib

from pydantic import ValidationError

from bae_pilot.config import load_settings
from bae_pilot.domain import AppError
from bae_pilot.storage.repositories import Store


def emit(value, *, stream=None):
    print(json.dumps(value, ensure_ascii=False), file=stream or sys.stdout)


async def database_command(settings, command):
    store = await Store.open(settings)
    try:
        return await store.status() if command == "status" else {"status": "initialized"}
    finally:
        await store.close()


def main(argv=None):
    parser = argparse.ArgumentParser(prog="baepilot", description="BaePilot 本地 API 与合成演示")
    parser.add_argument("--config", help="TOML 配置路径")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("demo", help="PostgreSQL 临时 schema 合成闭环，不读取外部模型配置")
    commands.add_parser("init-db", help="初始化数据库（不调用模型）")
    commands.add_parser("status", help="数据库状态；运行服务时使用 HTTP 状态接口")
    serve = commands.add_parser("serve", help="启动单进程本地管理 API")
    serve.add_argument("--host", choices=["127.0.0.1", "::1"], default="127.0.0.1")
    serve.add_argument("--port", type=int, choices=range(1, 65536), default=8765, metavar="PORT")
    args = parser.parse_args(argv)
    try:
        if args.command == "demo":
            from bae_pilot.demo import run_demo

            emit(asyncio.run(run_demo()))
        else:
            settings = load_settings(args.config)
            if args.command == "serve":
                import uvicorn

                from bae_pilot.app import create_app

                uvicorn.run(
                    create_app(settings),
                    host=args.host,
                    port=args.port,
                    workers=1,
                    access_log=False,
                )
            else:
                emit(asyncio.run(database_command(settings, args.command)))
        return 0
    except (ValidationError, tomllib.TOMLDecodeError):
        emit({"error": {"code": "validation_error"}}, stream=sys.stderr)
        return 2
    except AppError as exc:
        emit({"error": {"code": exc.code}}, stream=sys.stderr)
        return 2
    except OSError:
        emit({"error": {"code": "local_io_error"}}, stream=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

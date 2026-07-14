#!/usr/bin/env python3
"""PluginCheck — AstrBot 插件测试 CLI 工具。

用法:
  python3 cli.py check <插件名>              # 静态分析
  python3 cli.py test <插件名>               # 全量测试 (normal)
  python3 cli.py test <插件名> --heavy       # 压力测试
  python3 cli.py test <插件名> --count 2000  # 自定义压力数量
  python3 cli.py test <插件名> --journal     # 附 systemd 日志监控

设计原则:
  - 子进程隔离: 插件崩溃不影响测试器
  - 同进程捕获: 所有异常直接 try/except, 不需要 journalctl
  - Mock LLM: 零 token 消耗
  - 自动清理: 临时数据测完即删
"""

from __future__ import annotations

import argparse
import datetime
import sys
from pathlib import Path

from checker import Checker
from runner import Runner

PLUGINS_DIR = Path("data/plugins")
LOG_DIR = Path(__file__).parent / "logs"


def cmd_check(args):
    checker = Checker(str(PLUGINS_DIR), args.plugin)
    report = checker.run_all()
    print(report.format())


def cmd_test(args):
    checker = Checker(str(PLUGINS_DIR), args.plugin)
    static = checker.run_all()
    print(static.format())

    if static.failed > 0:
        print("\n❌ 静态分析未通过, 跳过运行时测试")
        sys.exit(1)

    runner = Runner(
        str(PLUGINS_DIR),
        args.plugin,
        heavy=args.heavy,
        count=args.count,
        tail_journal=args.journal,
    )
    report = runner.run_all()
    print(report.format())


def cmd_logs(args):
    """列出历史日志。"""
    if not LOG_DIR.is_dir():
        print("(暂无日志)")
        return
    files = sorted(LOG_DIR.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        print("(暂无日志)")
        return
    n = args.n
    for f in files[:n]:
        size = f.stat().st_size
        mtime = datetime.datetime.fromtimestamp(f.stat().st_mtime).strftime("%m-%d %H:%M")
        print(f"  {mtime}  {size:>6}B  {f.name}")


def main():
    p = argparse.ArgumentParser(description="PluginCheck — AstrBot 插件测试工具")
    sub = p.add_subparsers(dest="cmd")

    c = sub.add_parser("check", help="静态分析")
    c.add_argument("plugin", help="插件名 (如 astrbot_plugin_echoer)")

    t = sub.add_parser("test", help="全量测试")
    t.add_argument("plugin", help="插件名")
    t.add_argument("--heavy", action="store_true", help="压力测试")
    t.add_argument("--count", type=int, default=200, help="压力消息数 (默认 200)")
    t.add_argument("--journal", action="store_true", help="附 systemd 日志监控")

    l = sub.add_parser("logs", help="查看历史日志")
    l.add_argument("-n", type=int, default=10, help="显示最近 N 条 (默认 10)")

    args = p.parse_args()
    if args.cmd == "check":
        cmd_check(args)
    elif args.cmd == "test":
        cmd_test(args)
    elif args.cmd == "logs":
        cmd_logs(args)
    else:
        p.print_help()


if __name__ == "__main__":
    main()

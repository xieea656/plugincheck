"""运行时测试 — 子进程隔离 + 压力 + 异常捕获 + 自动清理。

核心思路:
  压力测试本身会暴露逻辑 bug — 并发竞态、索引崩溃、
  内存泄漏 最终都表现为异常。在子进程里全抓到。
"""

from __future__ import annotations

import asyncio
import datetime
import json as _json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Any

from checker import Report


def _time_ms() -> float:
    return time.perf_counter() * 1000


# ═══════════════════════════════════════════════════════════════
# 子进程 worker: 加载插件 → 初始化 → 接收测试指令 → 返回结果
# ═══════════════════════════════════════════════════════════════

WORKER_CODE = r'''
import asyncio, importlib, json, os, sys, time, traceback, gc, tempfile, shutil
from pathlib import Path

def _time(): return time.perf_counter() * 1000

async def _run():
    config = json.loads(sys.stdin.readline())
    plugin_dir = Path(config["plugin_dir"])
    mock_dim = config.get("embedding_dim", 1024)
    test_count = config.get("count", 200)
    heavy = config.get("heavy", False)
    parent = str(plugin_dir.parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    name = plugin_dir.name

    # ── 导入 ──
    module = importlib.import_module(f"{name}.main")
    StarClass = None
    for attr in dir(module):
        obj = getattr(module, attr)
        if isinstance(obj, type) and hasattr(obj, "_star_metadata"):
            meta = getattr(obj, "_star_metadata", None)
            if meta is not None:
                StarClass = obj; break

    if StarClass is None:
        print(json.dumps({"ok":False,"error":"no @register class"}), flush=True)
        return

    # ── Mock Context ──
    from plugincheck_mock import MockProvider
    mock = MockProvider(embedding_dim=mock_dim)

    class Ctx:
        def __init__(self): self.mock = mock
        async def get_using_provider(self, _=None): return None
        async def llm_generate(self, **kw):
            resp = await self.mock.llm_generate(**kw)
            return resp
        async def get_all_embedding_providers(self): return []
        def get_config(self, k, d=None): return d
        def add_llm_tools(self, *a, **kw): pass
        def register_llm_tool(self, **kw): pass

    # ── Fake Event ──
    class SimEvt:
        def __init__(s, text="", sid="test_user", gid="", plat="aiocqhttp"):
            s.message_str = str(text); s._sid = str(sid); s._gid = str(gid) if gid else ""
            s._plat = str(plat); s.unified_msg_origin = f"{plat}:{sid}"
            s.results = []; s._stopped = False
        def get_sender_id(s): return s._sid
        def get_sender_name(s): return "TestUser"
        def get_group_id(s): return s._gid
        def get_platform_name(s): return s._plat
        def is_private_chat(s): return not s._gid
        def plain_result(s, t):
            s.results.append(str(t))
            class R: pass
            return R()
        def stop_event(s): s._stopped = True
        async def send(s, r):
            s.results.append(str(getattr(r,"text",r)))

    plugin = StarClass(Ctx(), {})
    if hasattr(plugin, "initialize"):
        await plugin.initialize()

    results = {"ok": True, "events": [], "exceptions": [], "llm_calls": 0, "emb_calls": 0}

    # ── 发现 handler ──
    handlers = {}
    for name in dir(plugin):
        if name.startswith("_"): continue
        fn = getattr(plugin, name)
        if not callable(fn): continue
        fa = getattr(fn, "_filter_attrs", None)
        if fa is not None:
            handlers[name] = fa

    results["discovered"] = [{"name": k, "type": str(v.get("type","unknown"))} for k,v in handlers.items()]

    # ── 消息边界测试 ──
    if "on_all_messages" in handlers:
        tests = [
            ("empty","",True), ("long_8kb","A"*8192,False),
            ("special","<script>alert(1)</script>'OR 1=1--",True),
            ("emoji","test \U0001f680\U0001f4bb",False),
            ("spaces","  \n \t ",False),
        ]
        for label, text, priv in tests:
            evt = SimEvt(text=text, gid="" if priv else "test_group")
            try:
                async for _ in plugin.on_all_messages(evt): pass
                results["events"].append({"test": f"boundary:{label}", "ok": True})
            except Exception as e:
                results["events"].append({"test": f"boundary:{label}", "ok": False, "error": str(e)[:200]})

    # ── 命令触发 ──
    for hname, hmeta in handlers.items():
        if hmeta.get("type") == "command":
            cmd_name = hmeta.get("command_name", hname)
            evt = SimEvt(text=f"/{cmd_name} help")
            try:
                async for _ in fn(evt): pass
                results["events"].append({"test": f"cmd:{cmd_name}", "ok": True, "response": evt.results[:1]})
            except Exception as e:
                results["events"].append({"test": f"cmd:{cmd_name}", "ok": False, "error": str(e)[:200]})

    # ── LLM hook ──
    if "on_llm_request" in handlers:
        class Req: pass
        r = Req(); r.system_prompt = "test"; r.prompt = "hello"; r.extra_user_content_parts = []
        try:
            await plugin.on_llm_request(SimEvt(text="hello"), r)
            results["events"].append({"test": "hook:llm_request", "ok": True,
                "injected": len(r.extra_user_content_parts) > 0})
        except Exception as e:
            results["events"].append({"test": "hook:llm_request", "ok": False, "error": str(e)[:200]})

    # ── 并发压力 ──
    if heavy and "on_all_messages" in handlers:
        async def _one(i):
            evt = SimEvt(text=f"stress msg #{i}", sid=f"user_{i%20}", gid="stress_group")
            async for _ in plugin.on_all_messages(evt): pass
            return len(evt.results)
        start = _time()
        tasks = [_one(i) for i in range(test_count)]
        gathered = await asyncio.gather(*tasks, return_exceptions=True)
        elapsed = _time() - start
        crashes = sum(1 for v in gathered if isinstance(v, Exception))
        results["stress"] = {"count": test_count, "crashes": crashes, "elapsed_ms": round(elapsed),
            "p50_ms": round(elapsed / test_count, 1)}

    results["llm_calls"] = mock.llm_generate.call_count
    results["emb_calls"] = mock.embedding.encode_count

    # ── 清理 ──
    if hasattr(plugin, "terminate"):
        try: await plugin.terminate()
        except: pass
    gc.collect()

    print(json.dumps(results, ensure_ascii=False), flush=True)

asyncio.run(_run())
'''


class Runner:
    """在子进程中加载并测试插件, 捕获所有异常。"""

    def __init__(self, plugins_dir: str, plugin_name: str,
                 heavy: bool = False, count: int = 200, tail_journal: bool = False):
        self.plugins_dir = Path(plugins_dir)
        self.name = plugin_name
        self.plugin_dir = self.plugins_dir / plugin_name
        self.heavy = heavy
        self.count = count
        self.tail_journal = tail_journal

    def run_all(self) -> Report:
        r = Report()
        r.section("运行时测试")

        if not self.plugin_dir.is_dir():
            r.fail(f"插件目录不存在: {self.plugin_dir}")
            return r

        # 1. 准备临时 mock 模块（注入到子进程的 sys.path）
        tmpdir = tempfile.mkdtemp(prefix="plugincheck_")
        mock_src = Path(__file__).parent / "mock_provider.py"
        shutil.copy(str(mock_src), str(Path(tmpdir) / "plugincheck_mock.py"))
        with open(Path(tmpdir) / "plugincheck_mock.py", "a") as f:
            f.write("\n")

        # 2. 构造子进程 payload
        payload = {
            "plugin_dir": str(self.plugin_dir.resolve()),
            "embedding_dim": 1024,
            "count": self.count,
            "heavy": self.heavy,
        }

        # 3. 启动子进程
        env = os.environ.copy()
        env["PYTHONPATH"] = tmpdir + os.pathsep + env.get("PYTHONPATH", "")
        env["PYTHONUNBUFFERED"] = "1"

        proc = subprocess.Popen(
            [sys.executable, "-c", WORKER_CODE],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=env, text=True,
        )

        # 4. 可选的 journalctl 尾随
        journal_proc = None
        if self.tail_journal:
            journal_proc = subprocess.Popen(
                ["journalctl", "--user", "-u", "astrbot", "-f", "-n", "0", "--no-pager"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            )

        out, err = proc.communicate(input=json.dumps(payload), timeout=max(120, self.count * 0.5 + 10))

        # 5. 解析结果
        try:
            data = json.loads(out.strip().split("\n")[-1])
        except (json.JSONDecodeError, IndexError):
            r.fail(f"子进程无有效输出\nstderr: {err[:500]}")
            data = {}

        # 6. 展示发现
        discovered = data.get("discovered", [])
        if discovered:
            for h in discovered:
                r.info(f"handler: {h['type']:12s} {h['name']}")

        # 7. 事件结果
        events = data.get("events", [])
        for ev in events:
            label = ev["test"]
            if ev.get("ok"):
                extra = ""
                if "response" in ev and ev["response"]:
                    extra = f" → {str(ev['response'][0])[:60]}"
                if "injected" in ev:
                    extra = f" (injected={'yes' if ev['injected'] else 'no'})"
                r.ok(f"{label}{extra}")
            else:
                r.fail(f"{label}: {ev.get('error', 'unknown')}")

        # 8. 压力结果
        stress = data.get("stress")
        if stress:
            c = stress["count"]
            cr = stress["crashes"]
            if cr == 0:
                r.ok(f"并发 {c} 条, 无崩溃 ({stress['elapsed_ms']}ms)")
            else:
                r.fail(f"并发 {c} 条, {cr} 崩溃")

        # 9. 异常
        excs = data.get("exceptions", [])
        if excs:
            for exc in excs:
                r.fail(str(exc)[:200])

        # 10. stderr (子进程崩溃/panic)
        if err.strip():
            for line in err.strip().split("\n")[-5:]:
                if "Traceback" in line or "Error" in line or "Exception" in line:
                    r.fail(f"stderr: {line.strip()[:120]}")

        # 11. journalctl
        if journal_proc:
            journal_proc.terminate()
            try:
                jout, _ = journal_proc.communicate(timeout=2)
                tb_lines = [l for l in jout.split("\n") if "Traceback" in l or "ERROR" in l]
                if tb_lines:
                    r.warn(f"journal 发现 {len(tb_lines)} 条异常日志")
                    for l in tb_lines[:3]:
                        r.warn(f"  {l.strip()[:120]}")
            except Exception:
                pass

        # 12. 写日志文件
        log_path = self._write_log(r, data, err, journal_proc)

        # 13. 清理
        r.info(f"LLM 调用: {data.get('llm_calls', 0)} 次 (Mock, 0 token)")
        r.info(f"Embedding 调用: {data.get('emb_calls', 0)} 次 (Mock)")
        r.info(f"日志: {log_path}")
        shutil.rmtree(tmpdir, ignore_errors=True)

        return r

    def _write_log(self, report: Report, data: dict, stderr: str, journal_proc) -> str:
        """写入完整日志: 报告 + 原始 JSON + stderr + journal tail。"""
        log_dir = Path(__file__).parent / "logs"
        log_dir.mkdir(exist_ok=True)

        ts = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        log_path = log_dir / f"{self.name}_{ts}.log"

        sections = []
        sections.append(f"# PluginCheck: {self.name}")
        sections.append(f"# Time: {ts}")
        sections.append(f"# Heavy: {self.heavy}  Count: {self.count}\n")

        sections.append("## Report\n")
        sections.append(report.format())

        sections.append("\n## Raw Data\n")
        sections.append(_json.dumps(data, indent=2, ensure_ascii=False, default=str))

        if stderr.strip():
            sections.append("\n## stderr\n")
            sections.append(stderr.strip())

        if journal_proc:
            try:
                jout, _ = journal_proc.communicate(timeout=2)
                if jout.strip():
                    sections.append("\n## journalctl\n")
                    sections.append(jout.strip())
            except Exception:
                sections.append("\n## journalctl\n(读取超时)\n")

        with open(log_path, "w", encoding="utf-8") as f:
            f.write("\n".join(sections))

        return str(log_path)

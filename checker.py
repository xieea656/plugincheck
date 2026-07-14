"""静态分析：metadata、依赖、语法、导入、schema。"""

from __future__ import annotations

import ast
import importlib
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Report:
    lines: list[str] = field(default_factory=list)
    passed: int = 0
    warned: int = 0
    failed: int = 0

    def ok(self, msg: str):
        self.passed += 1
        self.lines.append(f"  ✅ {msg}")

    def warn(self, msg: str):
        self.warned += 1
        self.lines.append(f"  ⚠️  {msg}")

    def fail(self, msg: str):
        self.failed += 1
        self.lines.append(f"  ❌ {msg}")

    def info(self, msg: str):
        self.lines.append(f"  ℹ️  {msg}")

    def section(self, title: str):
        self.lines.append(f"\n📦 {title}")

    def format(self) -> str:
        total = self.passed + self.warned + self.failed
        header = "━━━━━━━━━━━━━━━━━━━━━━━━━━"
        body = "\n".join(self.lines)
        summary = f"  通过: {self.passed}  警告: {self.warned}  失败: {self.failed}"
        return f"{header}\n{body}\n{header}\n{summary}\n"


class Checker:
    def __init__(self, plugins_dir: str, plugin_name: str):
        self.plugins_dir = Path(plugins_dir)
        self.name = plugin_name
        self.plugin_dir = self.plugins_dir / plugin_name

    def run_all(self) -> Report:
        r = Report()

        r.section("静态分析")
        self._check_exists(r)
        if r.failed > 0:
            return r

        self._check_metadata(r)
        self._check_syntax(r)
        self._check_import(r)
        self._check_schema(r)
        self._check_deps(r)
        self._check_smells(r)
        return r

    def _check_exists(self, r: Report):
        if self.plugin_dir.is_dir():
            r.ok(f"目录存在: {self.plugin_dir}")
        else:
            r.fail(f"目录不存在: {self.plugin_dir}")

    def _check_metadata(self, r: Report):
        path = self.plugin_dir / "metadata.yaml"
        if not path.exists():
            r.fail("metadata.yaml 缺失")
            return
        try:
            import yaml
            with open(path, "r", encoding="utf-8") as f:
                meta = yaml.safe_load(f)
            required = ["name", "desc", "version", "author"]
            for key in required:
                if not meta.get(key):
                    r.warn(f"metadata.{key} 为空")
            if "astrbot_version" in meta:
                r.info(f"astrbot {meta['astrbot_version']}")
            platforms = meta.get("support_platforms", [])
            if platforms:
                r.info(f"平台: {', '.join(platforms)}")
            if not Path(self.plugin_dir / "CHANGELOG.md").exists():
                r.warn("CHANGELOG.md 缺失")
            r.ok("metadata.yaml 格式正确")
        except Exception as e:
            r.fail(f"metadata.yaml 解析失败: {e}")

    def _check_syntax(self, r: Report):
        count = 0
        errors = []
        for root, dirs, files in os.walk(self.plugin_dir):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d != "__pycache__"]
            for f in files:
                if not f.endswith(".py"):
                    continue
                count += 1
                try:
                    with open(os.path.join(root, f), "r", encoding="utf-8") as fh:
                        ast.parse(fh.read())
                except SyntaxError as e:
                    errors.append(f"{f}:{e.lineno} {e.msg}")
        if errors:
            for err in errors[:5]:
                r.fail(f"语法错误: {err}")
            if len(errors) > 5:
                r.fail(f"... 还有 {len(errors) - 5} 个")
        else:
            r.ok(f"语法无错误 ({count} 文件)")

    def _check_import(self, r: Report):
        main = self.plugin_dir / "main.py"
        if not main.exists():
            r.fail("main.py 缺失")
            return
        try:
            spec = importlib.util.spec_from_file_location(
                f"{self.name}.main", str(main),
                submodule_search_locations=[str(self.plugin_dir)]
            )
            r.ok("插件可导入")
        except Exception as e:
            r.fail(f"导入失败: {e}")

    def _check_schema(self, r: Report):
        schema_path = self.plugin_dir / "_conf_schema.json"
        if not schema_path.exists():
            r.info("无 _conf_schema.json（无配置项）")
            return
        try:
            with open(schema_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                r.ok("conf_schema JSON 有效")
                # 检查是否有明文密钥
                raw = json.dumps(data)
                lower = raw.lower()
                for kw in ("api_key", "apikey", "secret", "token", "password"):
                    for val in data.values() if isinstance(data, dict) else []:
                        if isinstance(val, dict):
                            for v in val.values():
                                if isinstance(v, str) and len(v) > 20 and kw in str(v).lower():
                                    r.warn(f"_conf_schema 包含可疑长字符串 (可能是 {kw})")
                                    break
            else:
                r.warn("conf_schema 根类型应为 object")
        except json.JSONDecodeError as e:
            r.fail(f"conf_schema JSON 无效: {e}")

    def _check_deps(self, r: Report):
        req_path = self.plugin_dir / "requirements.txt"
        if not req_path.exists():
            r.info("无 requirements.txt")
            return
        with open(req_path, "r", encoding="utf-8") as f:
            lines = [l.strip() for l in f if l.strip() and not l.startswith("#")]
        ok = 0
        missing = 0
        for line in lines:
            pkg = line.split(">=")[0].split("==")[0].split("<")[0].split(";")[0].strip()
            try:
                importlib.import_module(pkg.replace("-", "_"))
                ok += 1
            except ImportError:
                missing += 1
                r.warn(f"依赖未安装: {pkg}")
        if ok > 0:
            r.ok(f"依赖满足 ({ok}/{ok + missing})")

    def _check_smells(self, r: Report):
        """代码坏味道扫描。"""
        bare_excepts = 0
        for root, dirs, files in os.walk(self.plugin_dir):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d != "__pycache__"]
            for f in files:
                if not f.endswith(".py"):
                    continue
                path = os.path.join(root, f)
                with open(path, "r", encoding="utf-8") as fh:
                    for lineno, line in enumerate(fh, 1):
                        if line.strip() == "except:":
                            bare_excepts += 1
                            if bare_excepts <= 3:
                                r.warn(f"{f}:{lineno} bare except: (应用 except Exception)")
        if bare_excepts == 0:
            r.ok("无 bare except")

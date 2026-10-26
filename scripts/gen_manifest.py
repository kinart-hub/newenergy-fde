# -*- coding: utf-8 -*-
"""重建 manifest.json 的部署清单（离线部署包的打包依据）。

为什么必须是脚本而不是手工维护：
这个清单上一次手工维护的结果是只声明了 7 个根目录文档，
而仓库有 151 个受版本控制的文件——lib/ 下一个都没写进去。
照那份清单打出来的离线部署包，一行代码都没有。

用法：
    python scripts/gen_manifest.py          # 重写 manifest.json 的清单字段
    python scripts/gen_manifest.py --check  # 只核对，不改文件（退出码非 0 表示缺件）

criterion: 运行时跑一次 analyze 真正需要的文件必须全在里面。
所谓"需要"，以 lib/ + scripts/ + knowledge/ + SKILL.md 为准——
少一个 .py 就 import 不起来，少一个 knowledge/*.json 就没有工艺知识。
"""

from __future__ import unicode_literals

import argparse
import io
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 运行时必需：跑一次 analyze 缺了就 import 不起来或没有工艺知识
RUNTIME_DIRS = ["lib", "scripts"]
RUNTIME_KNOWLEDGE = ["knowledge"]
RUNTIME_FILES = ["SKILL.md", "manifest.json"]

# 随包交付的合规与说明文档
DOC_FILES = ["README.md", "CHANGELOG.md", "LICENSE", "NOTICE",
             "CLA.md", "CONTRIBUTING.md", "SECURITY.md", ".gitattributes",
             ".gitignore"]

# 永不得进清单：版本控制内部数据、构建/运行期产物、备份
EXCLUDE_DIR_PREFIX = (
    ".git/", "__pycache__/", "output/", "dist/", "tenants/",
    "_lineage/", ".workbuddy/",
)
EXCLUDE_SUFFIX = (".pyc", ".tmp", ".bak", ".pyo")

# 运行期按需生成，首次部署允许不存在
OPTIONAL_RUNTIME = [
    "knowledge/inbox.jsonl",
    "knowledge/backups",
    "knowledge/index.json.lock",
]

# 示例与社区物料：不影响离线可用性
EXTRA_DIRS = ["examples", "promotion", "assets", "docs", ".github"]


def tracked_files():
    try:
        out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                             text=True, encoding="utf-8", errors="replace")
        return [l.strip().replace("\\", "/")
                for l in (out.stdout or "").splitlines() if l.strip()]
    except Exception:
        return None


def walk_files():
    """git 不可用（比如在打好的包里）时退回文件系统遍历。"""
    acc = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        rel_dir = os.path.relpath(dirpath, ROOT).replace("\\", "/")
        if rel_dir == ".":
            rel_dir = ""
        dirnames[:] = [d for d in dirnames
                       if d not in (".git", "__pycache__", "output", "dist",
                                    "tenants", "_lineage", ".workbuddy")]
        for fn in filenames:
            rel = (rel_dir + "/" + fn) if rel_dir else fn
            acc.append(rel)
    return acc


def is_excluded(rel):
    if any(rel.startswith(p) or ("/" + p) in ("/" + rel) for p in EXCLUDE_DIR_PREFIX):
        return True
    if "/__pycache__/" in ("/" + rel) or rel.endswith(EXCLUDE_SUFFIX):
        return True
    # 备份副本要按"含 .bak 即排除"来判，不能只比对后缀：
    # 实际产物叫 device_profiles.json.bak-20261010195556，后缀是带时间戳的一串，
    # endswith(".bak") 匹配不到，于是它被当成"运行时必需文件"把发布卡住。
    if ".bak" in rel.lower():
        return True
    return False


def build_plan():
    """取「版本控制 ∪ 文件系统」的并集。

    只用 git ls-files 会漏掉还没 add 的新文件——lib/contract.py 就是这样漏过一次的：
    文件确实躺在磁盘上、确实是运行时必需，却因为还没进暂存区而不在清单里。
    只用 os.walk 又会把 .gitignore 掉的本地产物算进来。
    所以两边取并集，再统一走 excluded 过滤。
    """
    tracked = set(tracked_files() or [])
    on_disk = set(walk_files())
    files = sorted((tracked | on_disk))
    files = [f for f in files if not is_excluded(f)]
    runtime, extra = [], []
    for f in files:
        top = f.split("/")[0]
        if f in RUNTIME_FILES or top in RUNTIME_DIRS or top in RUNTIME_KNOWLEDGE:
            runtime.append(f)
        elif top in EXTRA_DIRS:
            extra.append(f)
    docs = [f for f in DOC_FILES if os.path.isfile(os.path.join(ROOT, f))]
    required = sorted(set(runtime) | set(docs))
    # extra 只记到目录：逐文件罗列会把示例里的客户代号写进清单，
    # 既踩脱敏规则，也让清单随示例增删不停漂移。
    extra_dirs = sorted({f.split("/")[0] + "/" for f in extra})
    return required, extra_dirs


def main(argv=None):
    ap = argparse.ArgumentParser(description="重建 manifest.json 的离线部署清单")
    ap.add_argument("--check", action="store_true",
                    help="只核对是否缺件，不改写 manifest.json")
    args = ap.parse_args(argv)

    os.chdir(ROOT)
    required, extra_dirs = build_plan()
    mpath = os.path.join(ROOT, "manifest.json")
    try:
        m = json.load(io.open(mpath, encoding="utf-8"))
    except Exception as exc:
        print("读不了 manifest.json：%s" % exc)
        return 2
    listed = set(m.get("repository_files") or [])

    missing = [f for f in required if f not in listed]
    ghost = [f for f in sorted(listed)
             if f not in OPTIONAL_RUNTIME and not os.path.exists(os.path.join(ROOT, f))]

    if args.check:
        if missing:
            print("缺 %d 个运行时文件，请运行 python scripts/gen_manifest.py 重建清单："
                  % len(missing))
            for f in missing[:10]:
                print("   -", f)
        if ghost:
            print("清单声明了 %d 个仓库里没有的文件：" % len(ghost))
            for f in ghost[:10]:
                print("   -", f)
        if not missing and not ghost:
            print("部署清单 OK：%d 项必需文件齐全，无悬空声明" % len(required))
            return 0
        return 1

    m["repository_files"] = required
    m["deployment"] = {
        "required": required,
        "optional": list(OPTIONAL_RUNTIME),
        "extra": extra_dirs,
        "excluded_patterns": list(EXCLUDE_DIR_PREFIX) + list(EXCLUDE_SUFFIX),
        "extra_note": ("附加物料按目录登记：示例/社区/推广物料不参与离线部署，"
                       "逐文件罗列会让清单随它们增删不断漂移。"),
        "note": ("required 为离线部署最小集：按它打包即可在只装 Python 标准库的环境"
                 "完成一次 analyze。optional 为运行期按需生成的文件，首次部署允许缺失。"
                 "extra 为示例与社区物料，缺了不影响运行。"
                 "本清单由 scripts/gen_manifest.py 从版本控制实况生成，"
                 "并由 scripts/prepublish_audit.py 的 check_manifest_coverage 每次发布前核对。"),
    }
    with io.open(mpath, "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print("已重建部署清单：必需 %d 项（lib=%d scripts=%d knowledge=%d）"
          % (len(required),
             sum(1 for x in required if x.startswith("lib/")),
             sum(1 for x in required if x.startswith("scripts/")),
             sum(1 for x in required if x.startswith("knowledge/"))))
    return 0


if __name__ == "__main__":
    sys.exit(main())

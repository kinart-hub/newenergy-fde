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

# 仓库侧治理工具：必须在仓库里（verify/CI/打包前置闸门都要跑它），
# 但**不随分发包外发**。
# 为什么单独列出来：prepublish_audit.py 内部带一整套「不许出现的东西」的正则字面量，
# 随包分发会污染接收方自己的隐私扫描。
# 为什么必须写进 manifest：原先只有 package.py 里一份硬编码排除清单，
# 而 manifest.deployment.required 仍把它算作「部署必需」，
# 于是清单声称 81 项、实际包内 80 项，按清单核验的人只会得到一个假缺件。
# 两个口径必须机器可对齐：repository_files = 仓库应齐全；deployment.required = 部署最小集。
REPO_ONLY_TOOLS = ["scripts/prepublish_audit.py"]

# 示例与社区物料：不影响离线可用性
EXTRA_DIRS = ["examples", "promotion", "assets", "docs", ".github"]


def code_version():
    """从 lib/pipeline.py 抽取 ANALYZER_VERSION，作为唯一版本真源。

    为什么不让 manifest.json 自己写版本：手工维护的派生数字一定会漂——
    本次实测 manifest.json 停在 2.10.0 而代码已是 2.19.1，打出来的离线包
    文件名直接错乱，且门禁全程静默。版本真源收敛到代码常量，重建清单时
    一并同步，谁都不用再记第二处数字。
    """
    import re
    p = os.path.join(ROOT, "lib", "pipeline.py")
    try:
        txt = io.open(p, encoding="utf-8").read()
    except Exception:
        return None
    m = re.search(r'ANALYZER_VERSION\s*=\s*["\']([^"\']+)["\']', txt)
    return m.group(1) if m else None


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
    # 仓库应齐全（repository_files）与部署最小集（deployment.required）分开：
    # 治理工具要在仓库里（verify 会跑），但不该进分发包。两者必须机器可对齐，
    # 否则清单说的是 81 项、包内只有 80 项，按清单核验的人拿到假缺件。
    not_deployed = sorted(set(required) & set(REPO_ONLY_TOOLS))
    deploy_required = [f for f in required if f not in set(not_deployed)]
    # extra 只记到目录：逐文件罗列会把示例里的客户代号写进清单，
    # 既踩脱敏规则，也让清单随示例增删不停漂移。
    extra_dirs = sorted({f.split("/")[0] + "/" for f in extra})
    return required, extra_dirs, not_deployed, deploy_required


def main(argv=None):
    ap = argparse.ArgumentParser(description="重建 manifest.json 的离线部署清单")
    ap.add_argument("--check", action="store_true",
                    help="只核对是否缺件，不改写 manifest.json")
    args = ap.parse_args(argv)

    os.chdir(ROOT)
    required, extra_dirs, not_deployed, deploy_required = build_plan()
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

    # 两个口径必须互相对得上：部署最小集 = 仓库应齐全 − 不随包外发的治理工具。
    # 原先只有 package.py 里一份硬编码排除清单，manifest 仍把 prepublish_audit.py
    # 算作「部署必需」，于是清单说 81 项、包内 80 项——按清单核验的人只会拿到假缺件。
    dep_listed = list((m.get("deployment") or {}).get("required") or [])
    dep_listed_set = set(dep_listed)
    deploy_drift = []
    if dep_listed:
        nd = set(not_deployed)
        allowed = set(required)
        for f in deploy_required:
            if f not in dep_listed_set and f in listed:
                # 只报"清单之间有漂移"的情形。清单外的文件（残留/误放）由上面的
                # missing 段报一条就够了，同一件事报两遍只会稀释真正要看的那条。
                deploy_drift.append(
                    "部署最小集漏了 %s（仓库清单有、部署集没跟上，重建清单即可）" % f)
        for f in dep_listed:
            if f not in allowed:
                deploy_drift.append("部署最小集多出 %s（仓库应齐全清单里没有）" % f)
            elif f in nd:
                deploy_drift.append("部署最小集含不随包外发的治理工具 %s" % f)

    cv = code_version()
    if cv and m.get("version") != cv:
        version_mismatch = True
    else:
        version_mismatch = False

    if args.check:
        if missing:
            # 不能只说「重建清单」：那会把残留垃圾一并登记成"必需文件"，
            # 等于把废弃物合法化。必须先让人分清这个文件是新加的、还是收拾漏的。
            print("扫描到 %d 个清单里没有的运行时文件，先确认它们该不该存在：" % len(missing))
            for f in missing[:10]:
                print("   -", f)
            print("   → 是新加的文件：运行 python scripts/gen_manifest.py 重建清单；")
            print("   → 是残留副本/误放：删掉它，不要登记进清单。")
        if ghost:
            print("清单声明了 %d 个仓库里没有的文件：" % len(ghost))
            for f in ghost[:10]:
                print("   -", f)
        if version_mismatch:
            print("版本不一致：manifest.json=%s 但代码 ANALYZER_VERSION=%s；"
                  "运行 python scripts/gen_manifest.py 重建清单以同步版本"
                  % (m.get("version"), cv))
        if deploy_drift:
            print("部署最小集与仓库清单不一致 %d 条（按它打包会缺件或多发）："
                  % len(deploy_drift))
            for d in deploy_drift[:10]:
                print("   -", d)
        if not missing and not ghost and not version_mismatch and not deploy_drift:
            print("部署清单 OK：仓库应齐全 %d 项、部署最小集 %d 项（治理工具 %d 项不随包外发），"
                  "无悬空声明，版本一致(%s)"
                  % (len(required), len(deploy_required), len(not_deployed),
                     m.get("version")))
            return 0
        return 1

    if cv:
        m["version"] = cv
    m["repository_files"] = required
    m["deployment"] = {
        "required": deploy_required,
        "not_deployed": not_deployed,
        "not_deployed_note": ("仓库侧治理工具：verify/CI 与打包前置闸门需要它，"
                              "但源码内含一整套『不许出现的东西』的正则字面量，"
                              "随包外发会污染接收方自己的隐私扫描。"
                              "故 required（部署最小集）不含它们；"
                              "打包侧须按 required 核对包内齐全性，且不得把 not_deployed 打进包。"),
        "optional": list(OPTIONAL_RUNTIME),
        "extra": extra_dirs,
        "excluded_patterns": list(EXCLUDE_DIR_PREFIX) + list(EXCLUDE_SUFFIX),
        "extra_note": ("附加物料按目录登记：示例/社区/推广物料不参与离线部署，"
                       "逐文件罗列会让清单随它们增删不断漂移。"),
        "note": ("repository_files 为仓库应齐全清单（含治理工具），用于核对仓库完整性；"
                 "required 为离线部署最小集（= repository_files − not_deployed）："
                 "按它打包即可在只装 Python 标准库的环境完成一次 analyze，"
                 "package.py 打包后按它强制核对包内齐全。"
                 "optional 为运行期按需生成的文件，首次部署允许缺失。"
                 "extra 为示例与社区物料，缺了不影响运行。"
                 "本清单由 scripts/gen_manifest.py 从版本控制实况生成，"
                 "并由 scripts/prepublish_audit.py 的 check_manifest_coverage 每次发布前核对。"),
    }
    with io.open(mpath, "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False, indent=2)
        f.write("\n")
    ver_note = "（版本已同步为 %s）" % m.get("version") if cv else ""
    print("已重建部署清单：仓库应齐全 %d 项、部署最小集 %d 项"
          "（lib=%d scripts=%d knowledge=%d；治理工具 %d 项不随包外发）%s"
          % (len(required), len(deploy_required),
             sum(1 for x in deploy_required if x.startswith("lib/")),
             sum(1 for x in deploy_required if x.startswith("scripts/")),
             sum(1 for x in deploy_required if x.startswith("knowledge/")),
             len(not_deployed), ver_note))
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""上架前隐私面与可上架性审计。

为什么要有这个脚本
------------------
第一次做发布前清理时，靠人工 grep 一遍：能查到，但**没有人会记得第二次做**。
更重要的是，团队里的下一手写 Makefile / CI 的人根本不知道有这回事。

所以把那次扫描固化成脚本：把「哪些东西不许出现在公开仓库里」写成可执行规则，
而不是写在我们俩的对话记录里。

规则分两级：
  BLOCK —— 发现即阻断，必须清理后才能发布（退出码 2）
  WARN  —— 可疑但可能是误报，需要人工确认（不阻断）

用法
----
    python scripts/prepublish_audit.py              # 审计本仓库
    python scripts/prepublish_audit.py --strict     # WARN 也当失败
    python scripts/prepublish_audit.py --json out   # 输出 JSON 供 CI 消费

建议接到 CI 里：`git push` 之前跑一遍。

注意：本脚本只检查**文本类**泄漏。二进制文件（xlsx / zip / 图片）里的
隐藏信息不在覆盖范围，发布前仍需人工看一遍二进制产物。
"""

import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# ---- 不参与打包/发布的目录：这些不在发布包里，法律意义上不放出去就无所谓，
#      但仍然扫一遍，因为总有一天会有人改 package.py 的排除列表。
LOCAL_ONLY_DIRS = {"output", "dist", "__pycache__", ".git", ".vscode", ".idea"}
LOCAL_ONLY_FILES_PAT = [
    re.compile(r"\.bak($|[-.])"),      # xxx.bak / xxx.bak-20261010
    re.compile(r"inbox\.jsonl$"),      # 知识候选队列（含现场来源）
]
LOCAL_ONLY_DIRS_PAT = [
    re.compile(r"^knowledge[\\/]backups$"),
    re.compile(r"^tenants[\\/][^\\/]+[\\/]knowledge$"),
]

# ---- 绝不许出现在公开仓库里的东西 ----
BLOCK_PATTERNS = [
    # 开发者本机路径：泄漏用户名、目录结构、内部项目名称
    (r"[A-Za-z]:[\\/]Users[\\/][A-Za-z0-9._-]+", "本机用户目录路径"),
    (r"[A-Za-z]:[\\/]workbuddy[\\/]", "本机 workbuddy 工作区路径"),
    (r"/home/[A-Za-z0-9._-]+/", "本机 home 路径"),

    # 凭证类：任一命中就当作已经失窃处理
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "私钥"),
    (r"ghp_[A-Za-z0-9]{20,}", "GitHub Personal Access Token"),
    (r"github_pat_[A-Za-z0-9_]{20,}", "GitHub Fine-grained Token"),
    (r"AKIA[0-9A-Z]{16}", "AWS Access Key"),
    (r"AIza[0-9A-Za-z_-]{35}", "Google API Key"),
    (r"xox[baprs]-[A-Za-z0-9-]{10,}", "Slack Token"),

    # 企业 / 个人标识（面向本项目场景的定制规则）
    (r"客户[一二三A-Z]\b", "客户代号（应替换为「某企业」或行业通用来源）"),
    (r"[\u4e00-\u9fa5]{2,6}(?:有限公司|股份有限公司|集团股份有限公司)", "企业名称"),
    (r"PG-\d{4}-\d{3}", "疑似企业内部工艺文件编号"),
    # 「工程师称谓」这条正则很容易误报：常见中文字面的组合会把它切出来，
    # 比如「任何工序」→何工、「多张工序表」→张工。所以要求姓氏前一个字符
    # 不是中文（真实用例通常写作 `"by": "王工"` 或 `—— 王工（`），
    # 并且排除后面紧跟「序」这种明显属于另一个词的情况。
    (r"(?<![\u4e00-\u9fff])[王李张刘陈杨黄赵吴周徐孙马朱胡林郭何高罗][工师总](?!序)",
     "疑似工程师称谓（如「王工」，应替换为「工艺工程师」）"),
]

# ---- 可疑但常见误报：提示人工确认 ----
WARN_PATTERNS = [
    (r"1[3-9]\d{9}", "疑似中国大陆手机号（也可能是数据里的随机数，请确认）"),
    (r"[\w.+-]+@[\w-]+\.[\w.]+", "邮箱地址"),
    (r"\b(?:\d{1,3}\.){3}\d{1,3}\b", "IP 地址"),
    (r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]\d|\d)", None),  # 占位，见下方剔除
]

# 排除掉纯数学常量里的巧合：小数里的连续数字常被误判，这里只在 WARN 阶段用
WARN_TEXT_EXTS = {".md", ".json", ".py", ".txt", ".csv", ".yml", ".yaml"}
SKIP_FILES = {"prepublish_audit.py"}     # 规则自身包含这些正则的字面量

# ---- 发布完整性：不是隐私问题，但少了它们仓库就不能算「可协作」 ----
REQUIRED_FILES = ["LICENSE", "NOTICE", "README.md", "CONTRIBUTING.md", ".gitignore"]
RECOMMENDED_FILES = ["CLA.md", "SECURITY.md", "CHANGELOG.md"]


def walk_files(root):
    """遍历待发布文件，跳过本地运行产物。"""
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root).replace("\\", "/")
        if rel_dir == ".":
            rel_dir = ""
        if any(rel_dir.startswith(d) or ("/" + d + "/") in ("/" + rel_dir + "/")
               for d in LOCAL_ONLY_DIRS):
            dirnames[:] = []
            continue
        if any(p.search(rel_dir) for p in LOCAL_ONLY_DIRS_PAT):
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames
                       if d not in LOCAL_ONLY_DIRS and not d.startswith(".")]
        for fn in filenames:
            if fn in SKIP_FILES:
                continue
            if fn.startswith(".") and fn != ".gitignore":
                continue
            if any(p.search(fn) for p in LOCAL_ONLY_FILES_PAT):
                continue
            yield os.path.join(dirpath, fn), os.path.join(rel_dir, fn) if rel_dir else fn


def scan():
    blocks, warns = [], []
    for full, rel in walk_files(ROOT):
        ext = os.path.splitext(full)[1].lower()
        try:
            with open(full, encoding="utf-8") as f:
                content = f.read()
        except (UnicodeDecodeError, OSError, IOError):
            continue      # 二进制或非 UTF-8：人工看，脚本不管

        for i, line in enumerate(content.splitlines(), 1):
            for pat, why in BLOCK_PATTERNS:
                m = re.search(pat, line)
                if m:
                    blocks.append({
                        "file": rel, "line": i, "rule": why,
                        "hit": m.group(0)[:60],
                        "snippet": line.strip()[:140],
                    })
            if ext in WARN_TEXT_EXTS:
                for pat, why in WARN_PATTERNS[:3]:     # 最后一条是占位
                    m = re.search(pat, line)
                    if m:
                        # 排除纯数值（小数/科学计数法）造成的误报
                        hit = m.group(0)
                        if re.fullmatch(r"[\d.]+([eE][+-]?\d+)?", hit):
                            continue
                        warns.append({
                            "file": rel, "line": i, "rule": why,
                            "hit": hit[:60], "snippet": line.strip()[:140],
                        })
    return blocks, warns


def check_library_hygiene():
    """改善措施库的业务闸门：演示条目不得参与推荐。

    这是从一次真实事故来的：库里躺了一条标记 can_recommend=true 的真实客户案例，
    意味着 Skill 会把某家客户的工艺参数直接推荐给别家工厂。
    现在代码里有硬闸门（kbstore.find_cases 跳过 demo），这里再验一次 JSON 侧。
    """
    issues = []
    p = os.path.join(ROOT, "knowledge", "improvement_library.json")
    if not os.path.exists(p):
        return issues
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        return [{"file": "knowledge/improvement_library.json",
                 "rule": "JSON 解析失败", "hit": str(e)}]
    for c in data.get("cases", []):
        cid = c.get("id", "?")
        if c.get("demo") and c.get("can_recommend"):
            issues.append({
                "file": "knowledge/improvement_library.json", "line": 0,
                "rule": "演示条目不得参与推荐（会被当成现场验证过的对策推给其他工厂）",
                "hit": cid, "snippet": "",
            })
        meta = (c.get("_kbmeta") or {}).get("source", "")
        if meta and not c.get("demo") and re.search(r"客户|[A-Za-z\u4e00-\u9fa5]+-[一-龥]{2,4}线", meta):
            issues.append({
                "file": "knowledge/improvement_library.json", "line": 0,
                "rule": "非演示案例带疑似具体企业来源，公开仓库不应收录",
                "hit": cid, "snippet": meta[:80],
            })
    return issues


def selfcheck():
    """给这套规则自己做体检。

    这些误报/漏报都真实发生过，所以每条都是当年踩过的坑：
    中文常见词（「任何工序」「多张工序表」）会被姓氏正则切出来；
    纯小数字串会被当成手机号。规则一旦改坏，这里立刻红。
    """
    cases = [
        # (样本行, 是否应被阻断)
        (r'"by": "李工",', True),
        (r'"by": "工艺工程师（已脱敏）",', False),
        (r'评：任何工序特征都没有识别到', False),      # 「任何工序」→ 何工
        (r'把多张工序表对齐成一张宽表', False),          # 「多张工序」→ 张工
        (r'source: C:/Users/Charlie/project/data.csv', True),
        (r'PG-2026-018', True),
        (r'PG-YYYY-NNN', False),
        (r'-----BEGIN RSA PRIVATE KEY-----', True),
        (r'6.680131188771972e+01', False),           # 正态分位数常量
        (r'客户B-涂布线', True),
        (r'某公司涂布线（已脱敏）', False),
    ]
    fails = []
    for text, should_block in cases:
        got = any(re.search(p, text) for p, _ in BLOCK_PATTERNS)
        if got != should_block:
            fails.append((text, should_block, got))
    return fails


def check_required():
    missing_required = [f for f in REQUIRED_FILES
                        if not os.path.exists(os.path.join(ROOT, f))]
    missing_rec = [f for f in RECOMMENDED_FILES
                   if not os.path.exists(os.path.join(ROOT, f))]
    return missing_required, missing_rec


def main(argv=None):
    ap = argparse.ArgumentParser(description="上架前隐私面与可上架性审计")
    ap.add_argument("--strict", action="store_true", help="WARN 也算失败")
    ap.add_argument("--json", metavar="PATH", help="额外输出 JSON 报告")
    ap.add_argument("--ignore-local-dirs", action="store_true",
                    help="连 output/、dist/ 等本地产物目录也一并扫描")
    ap.add_argument("--selfcheck", action="store_true",
                    help="给规则自己做体检：验证已知真命中/已知误报都判定正确")
    args = ap.parse_args(argv)

    if args.selfcheck:
        print("=" * 72)
        print("审计规则自检")
        print("=" * 72)
        fails = selfcheck()
        for text, want, got in fails:
            print("  ✗ 期望 %s，实际 %s ：%r" % ("阻断" if want else "放行",
                                            "阻断" if got else "放行", text))
        print("-" * 72)
        print("样本 %d 条，失败 %d 条：%s"
              % (11, len(fails), "FAIL" if fails else "PASS"))
        print("-" * 72)
        return 2 if fails else 0

    if args.ignore_local_dirs:
        LOCAL_ONLY_DIRS.clear()

    blocks, warns = scan()
    blocks.extend(check_library_hygiene())
    miss_req, miss_rec = check_required()

    only_warnings = list(warns)
    fatal = list(blocks)

    print("=" * 72)
    print("上架前审计")
    print("=" * 72)

    if fatal:
        print("\n【阻断】以下条目必须清理后才能发布：")
        seen = set()
        for it in fatal:
            key = (it["file"], it["rule"], it["hit"])
            if key in seen:
                continue
            seen.add(key)
            loc = "%s:%s" % (it["file"], it["line"]) if it.get("line") else it["file"]
            print("  ✗ %s" % loc)
            print("      规则：%s" % it["rule"])
            if it.get("hit"):
                print("      命中：%r" % it["hit"])
    else:
        print("\n✓ 未发现必须阻断的隐私面泄漏")

    if only_warnings:
        print("\n【提醒】以下条目需人工确认（可能是误报）：")
        seen = set()
        for it in only_warnings[:30]:
            key = (it["file"], it["line"], it["hit"])
            if key in seen:
                continue
            seen.add(key)
            print("  · %s:%s  %s  命中 %r"
                  % (it["file"], it["line"], it["rule"], it["hit"]))
        if len(only_warnings) > 30:
            print("  …… 另有 %d 条未列出" % (len(only_warnings) - 30))

    if miss_req:
        print("\n【阻断】缺少开源仓库必备文件：")
        for f in miss_req:
            print("  ✗ %s" % f)
    else:
        print("\n✓ 必备文件齐全（LICENSE / NOTICE / README / CONTRIBUTING / .gitignore）")

    if miss_rec:
        print("【建议】缺少推荐文件：%s" % "、".join(miss_rec))

    print("\n" + "-" * 72)
    failed = bool(fatal) or bool(miss_req)
    if args.strict:
        failed = failed or bool(only_warnings)
    print("阻断项 %d | 提醒项 %d | 结果：%s"
          % (len(fatal), len(only_warnings), "FAIL" if failed else "PASS"))
    print("-" * 72)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({"blocks": fatal, "warnings": only_warnings,
                       "missing_required": miss_req,
                       "missing_recommended": miss_rec},
                      f, ensure_ascii=False, indent=2)
        print("JSON 报告：%s" % args.json)

    return 2 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

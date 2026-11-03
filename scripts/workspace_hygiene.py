# -*- coding: utf-8 -*-
"""workspace_hygiene.py —— 工作区垃圾的机器判据（门禁第 11 步）

## 为什么需要这个工具

用户已多次要求"及时清理缓存、垃圾、无效数据，不要污染完成好的项目"。
反复没做到的原因不是忘了，是**这件事一直靠人肉 ls 加记性**：
每轮我只清了 `newenergy-fde/output/`（项目内部），而工作区根上的
`_audit_scratch/`、`_prepublish_backup/` 从 10 月 12 日一直躺到 11 月 3 日没人碰。
没有机器判据的事，就是没有人负责的事。

所以正确解法不是"这次记得清干净"，而是把判据固化成门禁项：
存量登记进基线（只许减不许增），**新增垃圾一律拦**。

## 判据边界（什么算垃圾，什么不算）

只判**客观可判**的，语义判断交给白名单并写明理由——这是刻意收窄的：
一个"很聪明"的判据一旦误删一次用户数据，就再也没人敢用这个工具。

  PY_CACHE   `__pycache__/`、`*.pyc`、`*.pyo`          —— 解释器缓存，删了会自动重建
  TEMP_FILE  `*.tmp`、`*~`、`*.swp`、`*.orig`、`*.bak*` —— 编辑器/原子写的残留
  STRAY_PY   工作区根上散落的一次性脚本（不在 scripts/ 等正式目录内）
  RESULT_ACCUM 同一目录下 `result*.json` ≥3 个          —— 多版本跑数堆积，只留最新
  SCRATCH_DIR 工作区根上 `_` 前缀的临时目录（是否可删需人工确认，本工具只报告不代判）

**不判**：任何含 `*.key` / `signer_key.json` / `PUBLIC_KEY.txt` 的目录（密钥）、
任何被 .gitignore 明确声明为"运行态/私密"的路径（`_private/`、`tenants/`）。
这些进白名单，白名单条目必须带理由——只写路径不写理由的条目视为无效，仍会报。

## 用法

    python scripts/workspace_hygiene.py              # 只报告，不动文件
    python scripts/workspace_hygiene.py --gate        # 门禁：新增即退出码 1
    python scripts/workspace_hygiene.py --json out.json
    python scripts/workspace_hygiene.py --update-baseline  # 收紧基线（只应减少）
    python scripts/workspace_hygiene.py --selftest   # 判据自检（会翻红才算有效）

**本工具永不删除任何文件**。发现即报告，删除动作由人执行——
"扫描器自动清理"是把自己变成风险源，与 personal_files_safety 冲突。
"""

from __future__ import print_function

import argparse
import json
import os
import re
import shutil
import sys
import tempfile

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
def _default_workspace(skill_root):
    """工作区根：开发时是 skill 的父目录；装在 skills 池里时就只用 skill 自身。

    为什么必须分开：skill 装在 `~/.workbuddy/skills/<name>/` 时，父目录是
    **所有 skill 的公共池**。拿它当工作区会把别人的 skill 目录一起扫进来，
    报出的垃圾既不是自己的、也轮不到自己去清——工具在安装位置跑就是错的。
    （实测：安装目录跑门禁时报了一条 `newenergy-fde/.trash/...`，
     路径是对的，但那次扫描范围已经越界到 skills/ 整个池子。）
    """
    parent = os.path.dirname(skill_root)
    if os.path.basename(parent).lower() in ("skills", "skill", ".skills"):
        return skill_root
    return parent


DEFAULT_WORKSPACE = _default_workspace(SKILL_ROOT)
BASELINE_REL = os.path.join("knowledge", "workspace_hygiene_baseline.json")

# 扫描时整棵跳过的目录（不是垃圾，是不该看的地方）。
# 注意 `__pycache__` **不在此列**：它是垃圾，要被抓到并报告；
# 首版误把它放进跳过集，结果"跳过"和"判为垃圾"互相抵消，
# 缓存既不被报告也不被跳过——判据静默失效（自检第 1 条就是这么翻红的）。
SKIP_DIRS = {".git", ".workbuddy", "node_modules", ".venv", "venv",
             ".idea", ".vscode", "dist", "output", ".pytest_cache", ".mypy_cache"}

# 密钥/私密特征：命中即整目录视为"必须保留"，不做任何垃圾判定
SECRET_MARKERS = (".key", "signer_key.json", "PUBLIC_KEY.txt", "license_key.json",
                  ".license_state.json")

PY_CACHE_DIR = "__pycache__"
PY_CACHE_EXT = (".pyc", ".pyo")
TEMP_EXT_RE = re.compile(r"(\.tmp$|~$|\.swp$|\.swo$|\.orig$|\.ORIG$|\.bak$|\.bak-|\.bak\.)",
                         re.IGNORECASE)

# 显式的历史归档目录：里面本来就该有同一工具的多版本产物，
# 再按"堆积"去罚它等于惩罚归档这个正确动作。
# 首版没排除时，刚把 6 个旧 result 移进 holdout/history/，判据立刻报了 5 条"堆积"。
ARCHIVE_DIR_RE = re.compile(r"(^|/)(history|archive|archived|_archive_[^/]*|归档)(/|$)",
                            re.IGNORECASE)


def _walk(workspace):
    """产出 (abs_path, rel_path, is_dir)。

    剪枝规则：SKIP_DIRS 整棵不进；`__pycache__` **报告目录本身但不深入**——
    否则一个依赖多的项目会刷出上千条 .pyc，告警一泛滥就没人看，
    而"这个目录该删"这件事报一条就够了。
    """
    for dirpath, dirnames, filenames in os.walk(workspace):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for d in list(dirnames):
            ap = os.path.join(dirpath, d)
            yield ap, os.path.relpath(ap, workspace).replace("\\", "/"), True
        # 已作为目录报过的缓存目录不再深入
        dirnames[:] = [d for d in dirnames if d != PY_CACHE_DIR]
        for f in filenames:
            ap = os.path.join(dirpath, f)
            yield ap, os.path.relpath(ap, workspace).replace("\\", "/"), False


def _has_secret(dirpath):
    """目录里（含一层子目录）是否存在密钥特征文件。"""
    try:
        for item in os.listdir(dirpath):
            if item.endswith(SECRET_MARKERS) or item in SECRET_MARKERS:
                return True
            sub = os.path.join(dirpath, item)
            if os.path.isdir(sub):
                try:
                    for s2 in os.listdir(sub):
                        if s2.endswith(SECRET_MARKERS) or s2 in SECRET_MARKERS:
                            return True
                except OSError:
                    # 读不到子目录就不能断言"里面没有密钥"。安全方向的判据
                    # 必须在不确定时偏向保守：判成"含密钥"只是少清一个目录，
                    # 判成"不含密钥"却可能把私钥当垃圾清掉。**这两种错的代价不对称。**
                    return True
    except OSError:
        # 同上：目录本身读不到时，一律按"可能含密钥"处理
        return True
    return False


def _load_baseline(skill_root):
    p = os.path.join(skill_root, BASELINE_REL)
    try:
        with open(p, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_baseline(skill_root, data):
    p = os.path.join(skill_root, BASELINE_REL)
    d = os.path.dirname(p)
    if d and not os.path.isdir(d):
        os.makedirs(d)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, p)


def scan(workspace=DEFAULT_WORKSPACE):
    """返回 {"findings": [...], "kept": [...]}。

    每条 finding: {kind, path, action, why, size?}
      action ∈ MUST_CLEAN（客观垃圾，可安全删）/ ARCHIVE（有价值但该移走）/ REVIEW（需人工判）
    """
    findings, kept = [], []
    result_dirs = {}

    for ap, rel, is_dir in _walk(workspace):
        if is_dir:
            if os.path.basename(ap) in (".trash",) or \
                    os.path.basename(ap).startswith(".trash-"):
                findings.append({"kind": "TRASH_DIR", "path": rel, "action": "MUST_CLEAN",
                                 "why": "明确的回收站目录：里面的东西已被判定不要了，"
                                        "留着只是把删除动作拖成了堆积"})
                continue
            if os.path.basename(ap) == PY_CACHE_DIR:
                findings.append({"kind": "PY_CACHE", "path": rel, "action": "MUST_CLEAN",
                                 "why": "解释器字节码缓存，删除后自动重建，不含任何源信息"})
                continue
            # 工作区根上的一级 `_` 前缀目录：只在最浅层报告，不深入逐个文件
            parent = os.path.dirname(rel)
            if parent == "" and os.path.basename(rel).startswith("_"):
                if _has_secret(ap):
                    kept.append({"path": rel, "action": "KEEP",
                                 "why": "含密钥/许可证文件，属私密资产，必须保留"})
                else:
                    sub = []
                    try:
                        sub = sorted(os.listdir(ap))[:6]
                    except OSError as _e:
                        # 列不出内容时报告必须显式说"读不到"，
                        # 否则空列表会被读成"这个目录是空的、可以放心删"
                        sub = ["（目录读取失败：%s）" % type(_e).__name__]
                    findings.append({"kind": "SCRATCH_DIR", "path": rel, "action": "REVIEW",
                                     "why": "工作区根上的临时/私有目录，需确认内容是否已固化到正式位置",
                                     "sample": sub})
                continue
            continue

        # 已整目录判为可删的回收站，里面的文件不再单独报：
        # 同一个事实报两次（`.trash` + `.trash/old.bak`）会让"共 N 项"虚高，
        # 而清掉 .trash 之后第二次报告又会少一条，读者会以为漏了东西。
        if any(seg == ".trash" or seg.startswith(".trash-")
               for seg in rel.split("/")[:-1]):
            continue
        base = os.path.basename(rel)
        ext = os.path.splitext(base)[1].lower()
        if ext in PY_CACHE_EXT:
            findings.append({"kind": "PY_CACHE", "path": rel, "action": "MUST_CLEAN",
                             "why": "解释器字节码文件"})
            continue
        if TEMP_EXT_RE.search(base):
            findings.append({"kind": "TEMP_FILE", "path": rel, "action": "MUST_CLEAN",
                             "why": "临时/备份后缀（.tmp/.bak/.ORIG/编辑器交换文件）"})
            continue
        # 工作区根上散落的脚本（正式脚本应进 scripts/）
        if os.path.dirname(rel) == "" and ext == ".py":
            findings.append({"kind": "STRAY_PY", "path": rel, "action": "REVIEW",
                             "why": "工作区根上的一次性脚本，正式脚本应固化进 scripts/ 并带 --check"})
        if re.match(r"^result.*\.json$", base, re.IGNORECASE):
            # 归档目录里的多版本是**有意保留的历史**，不是堆积
            if not ARCHIVE_DIR_RE.search(rel):
                result_dirs.setdefault(os.path.dirname(rel), []).append(rel)

    # 同目录 result*.json 堆积：只留最新一个
    for d, items in result_dirs.items():
        if len(items) < 3:
            continue
        def _mt(rel):
            try:
                return os.path.getmtime(os.path.join(workspace, rel))
            except OSError:
                return 0
        items_sorted = sorted(items, key=_mt, reverse=True)
        for rel in items_sorted[1:]:
            findings.append({"kind": "RESULT_ACCUM", "path": rel, "action": "ARCHIVE",
                             "why": "同一目录 %d 个 result*.json 多版本堆积，只保留最新（%s）"
                                    % (len(items), items_sorted[0])})
    return {"findings": findings, "kept": kept}


def _sig(f):
    """棘轮签名：只认 kind + path。不含 size/mtime——它们会天天变，进签名等于天天报警。"""
    return "%s|%s" % (f["kind"], f["path"])


def evaluate(workspace=DEFAULT_WORKSPACE, skill_root=SKILL_ROOT):
    res = scan(workspace)
    base = _load_baseline(skill_root)
    known = set(base.get("known") or [])
    cur = [_sig(f) for f in res["findings"]]
    new = [s for s in cur if s not in known]
    fixed = [s for s in known if s not in cur]
    return {"findings": res["findings"], "kept": res["kept"], "known": known,
            "current": cur, "new": new, "fixed": fixed}


# ------------------------------------------------------------------ 报告

_ACTION_LABEL = {"MUST_CLEAN": "可直接删", "ARCHIVE": "归档保留", "REVIEW": "需人工确认"}
_ACTION_ORDER = ("MUST_CLEAN", "ARCHIVE", "REVIEW")


def report(ev, as_json=None):
    print("=" * 72)
    print("工作区卫生检查 —— 判据：脚本注释里写明每一类的删除理由")
    print("=" * 72)
    fs = ev["findings"]
    if not fs:
        print("\n✓ 未发现缓存/临时残留/产物堆积")
    for action in _ACTION_ORDER:
        group = [f for f in fs if f["action"] == action]
        if not group:
            continue
        print("\n【%s】%d 项" % (_ACTION_LABEL[action], len(group)))
        for f in group:
            print("  · [%s] %s" % (f["kind"], f["path"]))
            print("      %s" % f["why"])
            if f.get("sample"):
                print("      目录内容示例：%s" % "、".join(f["sample"]))
    if ev["kept"]:
        print("\n【必须保留】%d 项（有理由，不算垃圾）" % len(ev["kept"]))
        for k in ev["kept"]:
            print("  · %s —— %s" % (k["path"], k["why"]))

    print("\n" + "-" * 72)
    print("棘轮判定：存量基线 %d 条｜当前 %d 条｜**新增 %d**｜已清掉 %d"
          % (len(ev["known"]), len(ev["current"]), len(ev["new"]), len(ev["fixed"])))
    if ev["new"]:
        print("  新增（必须先清理或登记进基线并写明理由）：")
        for s in ev["new"][:20]:
            print("    + %s" % s)
    if ev["fixed"]:
        print("  已清掉（可 --update-baseline 收紧基线）：")
        for s in ev["fixed"][:20]:
            print("    - %s" % s)
    if not ev["new"]:
        print("  ✓ 无新增垃圾")
    if as_json:
        with open(as_json, "w", encoding="utf-8") as fh:
            json.dump(ev, fh, ensure_ascii=False, indent=2)
        print("\n机读结果：%s" % as_json)


# ------------------------------------------------------------------ 自检

def _selftest():
    """判据自检：每个断言都必须**能翻红**。

    做法是在临时目录里造一个"假工作区"，逐类投放样本，再逐类把样本删掉
    看判据是否随之消失。只断言"能抓到"而不验"删掉后不再报"，
    会把一个"永远报有"的坏判据判成合格。
    """
    out = []
    tmp = tempfile.mkdtemp(prefix="hyg-")
    skill = os.path.join(tmp, "skill")
    os.makedirs(os.path.join(skill, "knowledge"))
    os.makedirs(os.path.join(tmp, "lib", "__pycache__"))
    try:
        # 样本投放
        open(os.path.join(tmp, "lib", "__pycache__", "x.cpython-313.pyc"), "w").close()
        open(os.path.join(tmp, "lib", "a.pyc"), "w").close()
        open(os.path.join(tmp, "notes.tmp"), "w").close()
        open(os.path.join(tmp, "kb.json.bak-20260101"), "w").close()
        open(os.path.join(tmp, "stray.py"), "w").close()
        os.makedirs(os.path.join(tmp, "_scratch"))
        open(os.path.join(tmp, "_scratch", "probe.py"), "w").close()
        os.makedirs(os.path.join(tmp, "_secret"))
        open(os.path.join(tmp, "_secret", "signer_key.json"), "w").close()
        os.makedirs(os.path.join(tmp, "hold"))
        for i in range(4):
            p = os.path.join(tmp, "hold", "result%s.json" % ("" if i == 0 else i))
            open(p, "w").close()
            os.utime(p, (1000 + i, 1000 + i))

        res = scan(tmp)
        kinds = {}
        for f in res["findings"]:
            kinds.setdefault(f["kind"], []).append(f["path"])

        out.append(("抓到字节码缓存（目录 + 文件）",
                    len(kinds.get("PY_CACHE", [])) == 2,
                    str(kinds.get("PY_CACHE"))))
        out.append(("抓到临时后缀（.tmp / .bak-）",
                    len(kinds.get("TEMP_FILE", [])) == 2,
                    str(kinds.get("TEMP_FILE"))))
        out.append(("抓到根目录散落脚本",
                    kinds.get("STRAY_PY") == ["stray.py"],
                    str(kinds.get("STRAY_PY"))))
        out.append(("抓到 _ 前缀临时目录",
                    kinds.get("SCRATCH_DIR") == ["_scratch"],
                    str(kinds.get("SCRATCH_DIR"))))
        # .trash 是明确的回收站，判为可直接删
        os.makedirs(os.path.join(tmp, ".trash"))
        open(os.path.join(tmp, ".trash", "old.bak-20260101"), "w").close()
        res_t = scan(tmp)
        out.append(("回收站目录 .trash 判为 MUST_CLEAN",
                    any(f["kind"] == "TRASH_DIR" and f["action"] == "MUST_CLEAN"
                        for f in res_t["findings"]),
                    str([f["kind"] for f in res_t["findings"]])))

        # 工作区根判定：装在 skills 池里就只用 skill 自身，不去扫别人的目录
        out.append(("装在 skills/ 下时工作区取 skill 自身（不越界扫别人的 skill）",
                    _default_workspace(os.path.join("X:", "s", "skills", "foo"))
                    == os.path.join("X:", "s", "skills", "foo"),
                    _default_workspace(os.path.join("X:", "s", "skills", "foo"))))
        out.append(("开发工作区取 skill 的父目录",
                    _default_workspace(os.path.join("X:", "work", "foo"))
                    == os.path.join("X:", "work"),
                    _default_workspace(os.path.join("X:", "work", "foo"))))

        out.append(("含密钥的目录判为保留而非垃圾",
                    [k["path"] for k in res["kept"]] == ["_secret"],
                    str([k["path"] for k in res["kept"]])))
        acc = kinds.get("RESULT_ACCUM", [])
        out.append(("result 堆积只留最新一个（4 个 → 报 3 个）",
                    len(acc) == 3 and "result0.json" not in acc,
                    str(acc)))

        # 变异：删掉全部临时后缀文件，判据必须随之不再报
        os.remove(os.path.join(tmp, "notes.tmp"))
        os.remove(os.path.join(tmp, "kb.json.bak-20260101"))
        res2 = scan(tmp)
        kinds2 = {}
        for f in res2["findings"]:
            kinds2.setdefault(f["kind"], []).append(f["path"])
        out.append(("删掉临时文件后 TEMP_FILE 归零（判据不是恒报）",
                    not kinds2.get("TEMP_FILE"),
                    str(kinds2.get("TEMP_FILE"))))

        # 变异：把 pyc 删掉后 PY_CACHE 归零
        shutil.rmtree(os.path.join(tmp, "lib", "__pycache__"))
        os.remove(os.path.join(tmp, "lib", "a.pyc"))
        res3 = scan(tmp)
        out.append(("删掉缓存后 PY_CACHE 归零",
                    not [f for f in res3["findings"] if f["kind"] == "PY_CACHE"], ""))

        # 棘轮：把当前全部登记进基线，再报新增应为 0
        b = {"known": [_sig(f) for f in res3["findings"]], "note": "selftest"}
        with open(os.path.join(skill, "knowledge", "workspace_hygiene_baseline.json"),
                  "w", encoding="utf-8") as fh:
            json.dump(b, fh, ensure_ascii=False)
        e1 = evaluate(tmp, skill)
        out.append(("棘轮：全部登记后新增为 0", not e1["new"], str(e1["new"])))
        # 再投一个新垃圾，必须被判为新增
        open(os.path.join(tmp, "another.tmp"), "w").close()
        e2 = evaluate(tmp, skill)
        out.append(("棘轮：新投一个垃圾即判新增",
                    len(e2["new"]) == 1 and "another.tmp" in e2["new"][0],
                    str(e2["new"])))
        # 签名不含易变字段（否则天天报警）
        out.append(("棘轮签名只含 kind|path，不含 size/mtime",
                    all("|" in s and "size" not in s and "mtime" not in s
                        for s in e2["current"] + e2["new"]),
                    str(e2["current"][:2])))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="工作区卫生检查（只报告，不删文件）")
    ap.add_argument("--workspace", default=DEFAULT_WORKSPACE,
                    help="工作区根，默认取 skill 的父目录")
    ap.add_argument("--gate", action="store_true", help="门禁模式：有新增垃圾则退出码 1")
    ap.add_argument("--json", default=None, help="机读结果写到该路径")
    ap.add_argument("--update-baseline", action="store_true", help="把当前状态登记为基线")
    ap.add_argument("--selftest", action="store_true", help="判据自检")
    args = ap.parse_args(argv)

    if args.selftest:
        print("=" * 72)
        print("工作区卫生判据自检")
        print("=" * 72)
        bad = 0
        for name, ok, detail in _selftest():
            print("  [%s] %s %s" % ("OK  " if ok else "FAIL", name, detail or ""))
            if not ok:
                bad += 1
        print("-" * 72)
        print("通过 %d / 失败 %d" % (len(_selftest()) - bad, bad))
        return 0 if bad == 0 else 1

    ev = evaluate(args.workspace, SKILL_ROOT)
    report(ev, args.json)

    if args.update_baseline:
        _save_baseline(SKILL_ROOT, {
            "note": "工作区卫生基线。只许减不许增——清掉一条就 --update-baseline 收紧一次。"
                    "签名 = kind|path，不含 size/mtime（它们会天天变，进签名等于天天报警）。",
            "known": sorted(set(ev["current"])),
        })
        print("\n基线已更新：%d 条（%s）"
              % (len(ev["current"]), os.path.join(SKILL_ROOT, BASELINE_REL)))
        return 0

    if args.gate and ev["new"]:
        print("\n✗ 工作区有新增垃圾 %d 条（存量不计）。清理后重跑，"
              "或 --update-baseline 登记并写明理由。" % len(ev["new"]))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

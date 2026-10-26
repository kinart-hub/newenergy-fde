#!/usr/bin/env python
"""把源仓库的改动同步到安装目录（运行时真正加载的位置）。

为什么需要这个脚本：
  项目有两处物理位置——源仓库（有 .git，推 GitHub）与安装目录
  （~/.workbuddy/skills/newenergy-fde，运行时加载）。两处不同步会导致
  「文档说的入口在运行时根本不存在」这类引用断链。

  此前靠手工列清单 cp，实测漏过 2 个文件（package.py / prepublish_audit.py）
  ——漏了之后安装目录的静态扫描读数与仓库不一致，才发现。
  手工清单不可靠的原因是：改了哪些文件这件事，git 已经知道，
  人不需要再记一遍。

用法：
  python scripts/sync_install.py                # 同步全部被 git 记录的改动
  python scripts/sync_install.py --all          # 同步全部受版本控制的文件（全量对齐）
  python scripts/sync_install.py --check        # 只核对，不写入
  python scripts/sync_install.py --dest <路径>  # 指定安装目录

约束（单向、不删）：
  - 只做 仓库 → 安装目录 的补齐，不反向；
  - 不动安装目录独有文件（tenants/、*.bak 等）；
  - 不删除安装目录里仓库没有的文件——删除需要人确认。
"""
import argparse
import io
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

DEFAULT_DEST = os.path.join(
    os.path.expanduser("~"), ".workbuddy", "skills", "newenergy-fde")

# 不同步的目录：安装目录里的租户数据、备份等运行时产物
SKIP_PREFIX = ("tenants/", "_private/", ".workbuddy/")
SKIP_SUFFIX = (".bak", ".pyc")


def _git(*args):
    r = subprocess.run(["git"] + list(args), cwd=ROOT,
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    if r.returncode != 0:
        raise RuntimeError("git %s 失败：%s" % (" ".join(args), r.stderr.strip()))
    return r.stdout


def changed_files():
    """git 记录的改动：已暂存 + 未暂存 + 未跟踪（排除 .gitignore 掉的）。"""
    names = []
    for line in _git("status", "--porcelain").splitlines():
        if len(line) < 4:
            continue
        flag, path = line[:2], line[3:].strip()
        # 重命名/复制的条目形如 `R  old -> new`，取后者
        if " -> " in path:
            path = path.split(" -> ")[-1]
        path = path.strip('"')
        # 目录条目（未跟踪的目录）展开成文件
        if os.path.isdir(os.path.join(ROOT, path)):
            for dp, dn, fn in os.walk(os.path.join(ROOT, path)):
                if ".git" in dp.split(os.sep):
                    continue
                for f in fn:
                    rel = os.path.relpath(os.path.join(dp, f), ROOT)
                    names.append(rel.replace("\\", "/"))
            continue
        names.append(path.replace("\\", "/"))
    return names


def tracked_files():
    out = []
    for line in _git("ls-files").splitlines():
        line = line.strip()
        if line:
            out.append(line.replace("\\", "/"))
    return out


def _skip(rel):
    if any(rel.startswith(p) for p in SKIP_PREFIX):
        return True
    if rel.endswith(SKIP_SUFFIX):
        return True
    if rel.startswith("output/") or rel.startswith(".git/"):
        return True
    return False


def main(argv=None):
    ap = argparse.ArgumentParser(description="同步源仓库改动到安装目录")
    ap.add_argument("--all", action="store_true",
                    help="同步全部受版本控制的文件（全量对齐，而非只看改动）")
    ap.add_argument("--check", action="store_true",
                    help="只核对两处是否一致，不写入")
    ap.add_argument("--dest", default=DEFAULT_DEST, help="安装目录路径")
    args = ap.parse_args(argv)

    dest = os.path.abspath(args.dest)
    if not os.path.isdir(dest):
        print("安装目录不存在：%s" % dest)
        print("若路径不同，用 --dest 指定。")
        return 1

    # ⚠️ 这里原先有个假阴性，改这段前先看清楚为什么：
    # 旧实现默认用 changed_files()（git status --porcelain）取"本次改动"。
    # 一旦 commit，改动就不在 status 里了 → rels 变空 →
    # 直接打印「没有需要同步的文件」并返回 0。
    # 实测后果：v2.4.0 提交后安装目录的 CHANGELOG.md 仍是旧版，
    # --check 却报「两处一致」。**报一致比报不一致危险得多**——
    # 它让人以为核对过了，而安装目录是真正被运行时加载的那一份。
    # 安装目录落后多少，跟"这次改了几个文件"根本没关系，
    # 所以核对与同步一律按全量受版本控制的文件走。
    rels = list(tracked_files())
    for r in changed_files():          # 还没 git add 的新文件也要带上
        if r not in rels:
            rels.append(r)
    rels = [r for r in rels if not _skip(r)]
    if not rels:
        print("仓库里没有受版本控制的文件，无法核对。")
        return 1

    same, diff, missing, copied = 0, [], [], 0
    for rel in sorted(set(rels)):
        src = os.path.join(ROOT, rel)
        dst = os.path.join(dest, rel)
        if not os.path.isfile(src):
            continue                      # 仓库里已删的文件：不反向删除
        if not os.path.isfile(dst):
            missing.append(rel)
        elif _same(src, dst):
            same += 1
            continue
        else:
            diff.append(rel)
        if not args.check:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            copied += 1

    print("=" * 66)
    print("源仓库：%s" % ROOT)
    print("安装目录：%s" % dest)
    print("-" * 66)
    print("  一致 %d 个文件" % same)
    if diff:
        print("  内容不同 %d 个：%s" % (
            len(diff), "、".join(diff[:8]) + ("…" if len(diff) > 8 else "")))
    if missing:
        print("  安装目录缺失 %d 个：%s" % (
            len(missing), "、".join(missing[:8])
            + ("…" if len(missing) > 8 else "")))
    if args.check:
        print("-" * 66)
        if diff or missing:
            print("结论：两处不一致，需要同步。")
            return 2
        print("结论：两处一致。")
        return 0
    print("-" * 66)
    print("已复制 %d 个文件。" % copied)
    print("注意：只做仓库→安装目录的单向补齐，未删除安装目录独有文件。")
    return 0


def _same(a, b):
    try:
        return (io.open(a, "rb").read() == io.open(b, "rb").read())
    except Exception:
        return False


if __name__ == "__main__":
    sys.exit(main())

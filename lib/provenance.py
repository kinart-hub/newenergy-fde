# -*- coding: utf-8 -*-
"""provenance.py —— 数据血缘与可复现记录（审计报告 P1-10 / 验收第 8 项）

报告原话：每次分析生成唯一 run_id，并保存原始文件哈希、导入时间、
解析器版本、字段映射版本、预处理规则版本、算法/参数、软件版本、
知识库版本、输入输出摘要、状态、日志和报告版本。
验收：开发者可以仅凭 run_id 重建本次分析的输入来源、配置、步骤和结论。

为什么单独建模块：
    此前报告第 17 节只写了「分析器版本 / 知识库版本 / 判异规则集」三行，
    够回答"用了哪个算法"，回答不了"喂的是什么数据、中间改过什么"。
    半年后有人拿报告问"这结论当时是对的吗"，三行答案是查不出来的。

run_id 与 fingerprint 分开设，刻意如此：
    fingerprint（内容寻址）= sha256(输入哈希 + 关键配置 + 版本)
        —— 同输入同版本必然得到同一个值。它用来回答"这次和上次是一回事吗"，
           也让"确定性步骤应得到一致结果"这句话变成可校验的断言。
    run_id = fingerprint[:12] + 墙钟短码
        —— 每次运行唯一，用来定位**这一轮**的证据文件。
    混成一个会两头不靠：只有 fingerprint 时同输入重跑会覆盖证据，
    只有随机数时又答不出"两次跑的是不是同一样东西"。
"""

import hashlib
import io
import json
import os
import time

LINEAGE_DIRNAME = "_lineage"
SCHEMA_VERSION = "1.0"


def sha256_file(path, chunk=1 << 20):
    """文件 sha256。读不出来返回 None（并记录原因），不抛给上层。"""
    try:
        h = hashlib.sha256()
        with io.open(path, "rb") as f:
            while True:
                b = f.read(chunk)
                if not b:
                    break
                h.update(b)
        return h.hexdigest()
    except Exception as exc:
        try:
            from . import degrade as DG
            DG.note("provenance.sha256_file",
                    "文件哈希算不出来（%s）：%s" % (os.path.basename(str(path)), exc))
        except Exception:
            pass                          # scan:keep 台账不可用时不得反向拖垮血缘采集
        return None


def input_fingerprints(paths):
    """输入清单：[{file, bytes, sha256, mtime}]，按文件名排序保证稳定。"""
    out = []
    for p in sorted(paths or []):
        try:
            st = os.stat(p)
            out.append({"file": os.path.basename(p),
                        "full_path": os.path.abspath(p),
                        "bytes": st.st_size,
                        "sha256": sha256_file(p),
                        "mtime": time.strftime("%Y-%m-%d %H:%M:%S",
                                               time.localtime(st.st_mtime))})
        except Exception as exc:
            out.append({"file": os.path.basename(str(p)),
                        "full_path": os.path.abspath(str(p)),
                        "error": "stat 失败：%s" % exc})
    return out


def _stable_json(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def fingerprint(inputs, config, versions):
    """内容寻址指纹：输入 + 配置 + 版本三者定。任一变化都会变。"""
    payload = {"inputs": inputs, "config": config, "versions": versions}
    return hashlib.sha256(_stable_json(payload).encode("utf-8")).hexdigest()


def new_run(inputs=None, config=None, versions=None, tenant=None,
            process=None, target=None):
    """生成一轮运行的血缘记录（未落盘）。"""
    inputs = inputs or []
    config = dict(config or {})
    versions = dict(versions or {})
    fp = fingerprint(inputs, config, versions)
    # 第三段用纳秒时间戳，不是装饰：
    #   只有秒级戳时，同一秒内的两次运行给出同一个 run_id，
    #   后一轮的血缘文件直接覆盖前一轮 —— 报告要求「每次分析生成唯一 run_id」
    #   就落空了（实测：测试里连调两次 new_run 立刻重复）。
    #   只加 pid 也不够，同一进程连续跑两次仍然撞车。
    run_id = "%s-%s-%s" % (fp[:12], time.strftime("%Y%m%d%H%M%S"),
                           str(time.time_ns())[-6:])
    return {
        "schema": SCHEMA_VERSION,
        "run_id": run_id,
        "fingerprint": fp,
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "tenant": tenant or "default",
        "process": process,
        "target": target,
        "inputs": inputs,
        "config": config,
        "versions": versions,
        "steps": [],
        "output_digest": {},
        "status": None,
        "warnings": [],
        "seed": None,
    }


def note_step(rec, name, status, seconds=None, detail=None):
    """记录一个步骤。一并记开工/收工时间戳，导出 yaml/md 时不至于靠猜。"""
    rec.setdefault("steps", []).append({
        "step": name,
        "status": status,
        "seconds": round(seconds, 3) if seconds is not None else None,
        "finished_at": time.strftime("%H:%M:%S"),
        "detail": detail or {},
    })


def note_nondeterminism(rec, seed=None, reason=None):
    """非确定性步骤必须交待随机种子或差异来源。

    报告要求：非确定性步骤必须记录随机种子或解释差异。不填就是埋雷——
    下次结果对不上时，连"是随机的还是改坏了"都分不清。
    """
    rec["seed"] = seed
    if reason:
        rec.setdefault("notes", []).append(reason)


def finish(rec, status, output_digest=None, outdir=None, warnings=None):
    """收尾并落盘 <outdir>/_lineage/<run_id>.json。

    写失败必须出声：血缘文件丢了一次，这一轮就在审计上等于没跑过。
    """
    rec["status"] = status
    rec["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    if output_digest:
        rec["output_digest"] = output_digest
    if warnings:
        rec.setdefault("warnings", []).extend(warnings)
    path = None
    if outdir:
        d = os.path.join(outdir, LINEAGE_DIRNAME)
        try:
            os.makedirs(d, exist_ok=True)
            path = os.path.join(d, "%s.json" % rec["run_id"])
            with io.open(path, "w", encoding="utf-8") as f:
                f.write(_stable_json(rec))
        except Exception as exc:
            try:
                from . import degrade as DG
                DG.note("provenance.finish",
                        "血缘记录落盘失败（%s）：%s" % (rec["run_id"], exc))
            except Exception:
                pass                      # scan:keep 同上：台账不可用不阻断交付
            path = None
    rec["lineage_file"] = path
    return rec


def load(run_id, outdir):
    """按 run_id 读回一条血缘记录。找不到返回 None（调用方要显式判断）。"""
    p = os.path.join(outdir, LINEAGE_DIRNAME, "%s.json" % run_id)
    if not os.path.exists(p):
        return None
    try:
        with io.open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def replay_check(rec, expected_versions=None):
    """复现自检：拿一条血缘记录，看现在能不能复现当时的输入与配置。

    返回 (ok, problems)。检查三件事：
      1. 输入文件还在不在、字节数/哈希对不对（改过就说明不是同一份数据了）
      2. 版本是否一致（知识库/分析器升级过，结论口径可能已变）
      3. run_id 是否与内容吻合（防手改）
    不能真跑一遍分析——那要重跑流水线；这里核的是"能不能重建输入来源与配置"，
    正是报告对 run_id 的验收口径。
    """
    problems = []
    if not isinstance(rec, dict):
        return False, ["血缘记录不是对象"]
    for it in rec.get("inputs", []):
        path = it.get("full_path")
        if not path or not os.path.exists(path):
            problems.append("输入文件已不存在：%s" % it.get("file"))
            continue
        try:
            if os.path.getsize(path) != it.get("bytes"):
                problems.append("输入文件字节数已变：%s" % it.get("file"))
            elif sha256_file(path) != it.get("sha256"):
                problems.append("输入文件内容已变（哈希不符）：%s" % it.get("file"))
        except Exception as exc:
            problems.append("输入文件无法复核：%s（%s）" % (it.get("file"), exc))
    exp = expected_versions or rec.get("versions") or {}
    now = dict(rec.get("versions") or {})
    for k, v in (exp or {}).items():
        if now.get(k) != v:
            problems.append("版本不一致：%s 当时=%s 现在=%s"
                            % (k, now.get(k), v))
    if rec.get("run_id"):
        # 取第一段而不是 rsplit：run_id 是 "<指纹12位>-<时间戳>-<纳秒>"，
        # 写成 rsplit 会把时间戳一起留下，然后拿去和纯 hex 的指纹比 startswith，
        # 永远不成立 —— 结果就是"每条记录都被判被篡改"，包括没动过的。
        base = str(rec["run_id"]).split("-")[0]
        fp = fingerprint(rec.get("inputs", []), rec.get("config", {}),
                         rec.get("versions", {}))
        if not fp.startswith(base):
            problems.append("run_id 与内容不符（记录被改动过？）")
    return (not problems), problems

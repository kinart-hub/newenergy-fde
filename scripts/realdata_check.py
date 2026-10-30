# -*- coding: utf-8 -*-
"""
realdata_check.py —— 真实数据印证回路（外部公开数据 → 印证技能是否符合行业要求）

=============================================================================
为什么要这个回路，以及它和 eval_enterprise 的分工
=============================================================================
    `eval_enterprise.py` 评的是**判得对不对**——它需要 truth.json（真值），
    而真值只有两种来源：企业自己给，或用生成器造。
    **从行业网站/论坛爬来的真实数据通常没有真值**，所以它验证不了"判得对不对"。

    那真实数据还能验证什么？三件同样重要、且只有真实数据才能验证的事：

      1. **吃得下吗**（健壮性）：真实导出文件的脏法千奇百怪——
         列名匿名化、缺失值、常数特征、科学计数法、行列不齐、文件被截断。
         仿真数据永远造不出这些"野"形态。
      2. **认得出来吗**（覆盖度）：知识库里的参数别名/缺陷叫法/工序名，
         面对真实字段能命中多少？命中不了多少，就说明知识库是照着自洽的
         仿真数据写的，行业实际对不上。
      3. **口径合不合**（校准）：真实值的量级/分布，落在知识库 `typical_range`
         里还是落在外面？落外面就意味着库里的"量级兜底"跟行业实际脱节。

    这三件事合起来，正好回答用户的问题：**"这个技能符合行业要求吗"**。

=============================================================================
铁律（比功能重要，改代码前先读）
=============================================================================
    1. **没有来源的数据一律拒收。** `source.json` 缺 url / 抓取时间 / 口径说明
       就直接退出码 2。理由很直白：一份说不清从哪来的数据，它的结论也无法追溯；
       而"数据来路不明"和"数据是编的"在下游看起来一模一样。
    2. **残缺数据可以用，但必须显式声明。** 实测踩过：代理把 UCI SECOM 的
       9MB 文件截断到 446KB，行列数全乱。若不校验，会把"截断"当成
       "这份数据只有 130 行"，然后得出"样本太少"的错误结论。
       声明 `truncated: true` + `truncation_note` 才允许继续，且报告里必须标红。
    3. **完整性用 sha256 + 行列数双校验。** 只校验一个不够：行数对得上但内容被
       改过（或压缩流部分解压）时，行数看不出问题。
    4. **不给"符合行业要求"的总结论。** 只给三档读数：能识别多少、
       多少落在库外、哪些字段完全认不出。结论由人下。
    5. **识别不出来是结论，不是失败。** 真实晶圆厂数据是**匿名列名**（X1..X590），
       按名字匹配注定 0 命中。这不是 bug，这是"真实数据没法靠列名映射"这一事实，
       应如实报出来，并指向正确的补法（设备档案/列映射访谈）。

=============================================================================
目录约定
=============================================================================
    realdata/<case>/
        data.csv            真实数据（或原样文件，data.* 均可）
        source.json         来源元数据（**必填**）
        truth.json          可选；有真值时才算"判得对不对"
        README.md           可选；人读的补充说明

    source.json 必填字段：
        source_url      数据出处（URL 或文献标识）
        retrieved_at    抓取日期 YYYY-MM-DD
        publisher       发布方
        caliber_note    口径说明（列名含义/单位/采样条件；匿名化也要写明）
    可选字段：
        license / declared_rows / declared_cols / sha256 /
        truncated / truncation_note / unit_note

用法：
    python scripts/realdata_check.py --root realdata
    python scripts/realdata_check.py --root realdata --case secom
    python scripts/realdata_check.py --root realdata --report output/realdata.md
"""

import argparse
import hashlib
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(HERE)
sys.path.insert(0, ROOT_DIR)

REQUIRED = ("source_url", "retrieved_at", "publisher", "caliber_note")
OPTIONAL = ("license", "declared_rows", "declared_cols", "sha256",
            "truncated", "truncation_note", "unit_note", "notes")

DATA_EXTS = (".csv", ".tsv", ".txt", ".xlsx")


def sha256_of(path):
    h = hashlib.sha256()
    with io.open(path, "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def find_data(case_dir):
    """在用例目录里找数据文件。找不到返回 None（不瞎猜）。"""
    cands = sorted(os.listdir(case_dir))
    for ext in DATA_EXTS:
        hits = [c for c in cands if c.lower().endswith(ext)]
        if hits:
            return os.path.join(case_dir, hits[0])
    return None


def check_source(case_dir):
    """校验 source.json。返回 (ok, meta, problems)。"""
    p = os.path.join(case_dir, "source.json")
    if not os.path.exists(p):
        return False, {}, ["缺 source.json —— 没有来源的数据一律拒收"
                           "（说不清来路的数据，其结论也无法追溯）"]
    try:
        meta = json.load(io.open(p, encoding="utf-8"))
    except Exception as e:
        return False, {}, ["source.json 解析失败：%s" % str(e)[:80]]
    problems = []
    for k in REQUIRED:
        v = meta.get(k)
        if v is None or (isinstance(v, str) and not v.strip()):
            problems.append("source.json 缺必填字段 %r" % k)
    # 日期格式
    ra = str(meta.get("retrieved_at") or "")
    if ra and not (len(ra) == 10 and ra[4] == "-" and ra[7] == "-"):
        problems.append("retrieved_at 不是 YYYY-MM-DD：%r" % ra)
    # 声明截断就必须给说明
    if meta.get("truncated") and not str(meta.get("truncation_note") or "").strip():
        problems.append("声明了 truncated=true 却没写 truncation_note —— "
                        "残缺到什么程度必须写清")
    # 未声明截断却给了 declared_* 时，后续还要做实际比对
    return (not problems), meta, problems


def integrity(case_dir, data_path, meta):
    """完整性校验：sha256 + 行列数。返回 (ok, info, problems)。"""
    info = {"file": os.path.basename(data_path) if data_path else None,
            "size": os.path.getsize(data_path) if data_path else 0,
            "sha256": sha256_of(data_path) if data_path else None,
            "rows": None, "cols": None}
    problems = []
    if not data_path:
        return False, info, ["用例目录里没有可识别的数据文件（%s）"
                             % "/".join(DATA_EXTS)]

    # 行数/列数：走项目自己的读表器，顺带验证它吃得下
    try:
        from lib import table as T
        tb = T.read_any(data_path)
        info["rows"], info["cols"] = len(tb.rows), len(tb.columns)
    except Exception as e:
        return False, info, ["读表失败：%s" % str(e)[:120]]

    # sha256 比对
    want = str(meta.get("sha256") or "").strip().lower()
    if want and info["sha256"] and want != info["sha256"]:
        problems.append("sha256 不符（声明 %s… 实际 %s…）：文件内容与声明不一致，"
                        "要么被改过、要么下载不完整"
                        % (want[:12], info["sha256"][:12]))
    # 行列数比对
    tr = meta.get("truncated")
    for key, got in (("declared_rows", info["rows"]), ("declared_cols", info["cols"])):
        declared = meta.get(key)
        if declared is None:
            continue
        try:
            d = int(declared)
        except (TypeError, ValueError):
            problems.append("%s 不是整数：%r" % (key, declared))
            continue
        if d != got:
            msg = ("%s 声明 %d，实际 %d" % (key, d, got))
            if tr:
                info.setdefault("mismatch_declared", []).append(msg)
            elif abs(d - got) <= max(2, int(0.02 * max(d, 1))):
                info.setdefault("mismatch_declared", []).append(msg + "（差异很小）")
            else:
                problems.append(msg + " —— 且未声明 truncated，"
                                      "先确认是不是下载被截断/解析错位")
    return (not problems), info, problems


def run_coverage(data_path):
    """跑一遍分析，得出「认得出来多少」的读数。

    只取识别侧读数，不评价判得对不对（那要真值）。
    """
    from lib import pipeline as PL
    from lib import preprocess as PP
    from lib import rootcause as RC

    out = {"read_ok": False, "analyze_ok": False, "cols": 0, "numeric_cols": 0,
           "mapped_params": 0, "mapped_cols": [], "unmapped_sample": [],
           "process": None, "process_name": None, "defects": [],
           "rootcause_candidates": 0, "elapsed_s": None, "notes": []}
    try:
        from lib import table as T
        tb = T.read_any(data_path)
    except Exception as e:
        out["notes"].append("读表失败：%s" % str(e)[:100])
        return out
    out["read_ok"] = True
    out["cols"] = len(tb.columns)
    try:
        out["numeric_cols"] = len(tb.numeric_columns())
    except Exception:
        out["numeric_cols"] = 0

    # 列 → 参数映射命中（这是覆盖度的核心读数）
    kb = PP.load_knowledge() or {}
    params = kb.get("params") or {}
    alias2key = {}
    for k, v in params.items():
        for a in ([v.get("name")] + list(v.get("aliases") or [])):
            if a:
                alias2key[str(a).strip().lower()] = k
    hit, miss = [], []
    for c in tb.columns:
        key = alias2key.get(str(c).strip().lower())
        if key:
            hit.append({"column": c, "param_key": key})
        else:
            miss.append(c)
    out["mapped_params"] = len(hit)
    out["mapped_cols"] = hit
    out["unmapped_sample"] = miss[:15]

    # 工序/缺陷识别（不传 process，让工具自己认）
    # analyze_ok 必须单独记：崩了和"跑了但一个都没认出"是**完全不同的结论**，
    # 混成一个会让"工具在这份数据上直接崩"被报成"仅统计可用"（自测时踩过）。
    import time as _t
    _t0 = _t.time()
    try:
        res = PL.analyze([data_path], outdir=os.path.join("output", "_realdata_probe"),
                         do_doe=False, max_charts=0)
        out["analyze_ok"] = not res.get("error")
        out["elapsed_s"] = round(_t.time() - _t0, 1)
        out["process"] = res.get("process")
        out["process_name"] = res.get("process_name")
        out["defects"] = [d.get("defect") for d in (res.get("defects") or [])]
        out["rootcause_candidates"] = len(
            (res.get("rootcause") or {}).get("candidates") or [])
        for w in (res.get("warnings") or [])[:5]:
            out["notes"].append(str(w)[:120])
    except Exception as e:
        out["analyze_ok"] = False
        out["elapsed_s"] = round(_t.time() - _t0, 1)
        out["analyze_error"] = "%s: %s" % (type(e).__name__, str(e)[:160])
        out["notes"].append("analyze 抛异常：%s" % out["analyze_error"])
    return out


def caliber_compare(data_path, limit=40):
    """把真实数据落在知识库 typical_range 内/外的情况统计出来。

    这是"印证是否符合行业要求"最硬的一条读数：
    库里的量级兜底如果跟真实数据大面积错位，说明它是照着仿真自洽写的。
    """
    from lib import table as T
    from lib import preprocess as PP
    from lib import stats as S

    res = {"compared": 0, "inside": 0, "outside": [], "no_range": 0}
    kb = PP.load_knowledge() or {}
    params = kb.get("params") or {}
    alias2key = {}
    for k, v in params.items():
        for a in ([v.get("name")] + list(v.get("aliases") or [])):
            if a:
                alias2key[str(a).strip().lower()] = k
    try:
        tb = T.read_any(data_path)
    except Exception:
        return res
    for ci, c in enumerate(tb.columns):
        key = alias2key.get(str(c).strip().lower())
        if not key:
            continue
        pv = params.get(key) or {}
        tr = pv.get("typical_range")
        if not (isinstance(tr, (list, tuple)) and len(tr) == 2):
            res["no_range"] += 1
            continue
        vals = []
        for r in tb.rows:
            v = T.to_number(r[ci]) if ci < len(r) else None
            if v is not None and S.is_num(v):
                vals.append(v)
        if not vals:
            continue
        lo, hi = float(tr[0]), float(tr[1])
        mn, mx = min(vals), max(vals)
        res["compared"] += 1
        if mn >= lo and mx <= hi:
            res["inside"] += 1
        else:
            res["outside"].append({
                "column": c, "param_key": key, "kb_range": [lo, hi],
                "data_range": [mn, mx], "n": len(vals)})
    return res


def assess(case_dir):
    """单个用例的完整印证。返回 dict。"""
    name = os.path.basename(case_dir.rstrip("/\\"))
    out = {"case": name, "ok": False, "problems": [], "warnings": [],
           "source": {}, "integrity": {}, "coverage": {}, "caliber": {},
           "verdict": None, "has_truth": os.path.exists(
               os.path.join(case_dir, "truth.json"))}
    ok, meta, p1 = check_source(case_dir)
    out["source"] = {k: meta.get(k) for k in list(REQUIRED) + list(OPTIONAL) if k in meta}
    if not ok:
        out["problems"].extend(p1)
        return out

    data_path = find_data(case_dir)
    ok2, info, p2 = integrity(case_dir, data_path, meta)
    out["integrity"] = info
    out["problems"].extend(p2)
    if not ok2:
        return out

    if meta.get("truncated"):
        out["warnings"].append(
            "该用例**声明为残缺数据**：%s —— 读数只能作健壮性参考，"
            "不可用于统计显著性或覆盖率结论"
            % str(meta.get("truncation_note") or "")[:120])
    if info.get("mismatch_declared"):
        out["warnings"].append("声明与实际不一致（已声明截断，故不阻断）：%s"
                               % "；".join(info["mismatch_declared"]))

    out["coverage"] = run_coverage(data_path)
    out["caliber"] = caliber_compare(data_path)

    cov = out["coverage"]
    if not cov.get("read_ok"):
        out["problems"].append("数据读不进来，无法印证")
        return out
    # analyze 崩了 → 直接判不可用。这是这份数据最有价值的信号（真实脏法打崩了工具），
    # 绝不能含糊成"仅统计可用"。
    if not cov.get("analyze_ok"):
        out["verdict"] = "不可用（analyze 抛异常，需修缺陷）"
        out["problems"].append("analyze 在该数据上抛异常：%s" % cov.get("analyze_error"))
        return out

    # 三档结论
    if cov["mapped_params"] == 0:
        out["verdict"] = "仅统计可用（列名无法映射到知识库参数）"
    elif cov["mapped_params"] < max(3, cov["cols"] // 10):
        out["verdict"] = "部分映射（命中 %d/%d 列）" % (cov["mapped_params"], cov["cols"])
    else:
        out["verdict"] = "映射良好（命中 %d/%d 列）" % (cov["mapped_params"], cov["cols"])
    out["ok"] = True
    return out


def render(reports, path=None):
    L = ["# 真实数据印证报告", "",
         "> 目的不是评「判得对不对」（那要真值），而是评三件事："
         "**吃得下吗 / 认得出来吗 / 口径合不合**。", ""]
    n_ok = sum(1 for r in reports if r["ok"])
    L += ["- 用例数：%d（通过来源与完整性校验 %d）" % (len(reports), n_ok), ""]
    for r in reports:
        L += ["## %s" % r["case"], ""]
        if r["problems"]:
            L += ["**拒收/阻断：**", ""]
            for p in r["problems"]:
                L += ["- ✗ %s" % p]
            L += [""]
            continue
        s = r["source"]
        L += ["| 项 | 值 |", "|---|---|"]
        L += ["| 来源 | %s |" % s.get("source_url")]
        L += ["| 发布方 | %s |" % s.get("publisher")]
        L += ["| 抓取日期 | %s |" % s.get("retrieved_at")]
        L += ["| 许可 | %s |" % (s.get("license") or "未标注")]
        L += ["| 口径 | %s |" % str(s.get("caliber_note"))[:160]]
        i = r["integrity"]
        L += ["| 文件 | %s（%.1f KB） |" % (i.get("file"), (i.get("size") or 0) / 1024.0)]
        L += ["| 规模 | %s 行 × %s 列 |" % (i.get("rows"), i.get("cols"))]
        L += ["| sha256 | `%s…` |" % str(i.get("sha256"))[:16]]
        L += [""]
        c = r["coverage"]
        L += ["**读数**", "",
              "- 工序识别：%s（%s）" % (c.get("process") or "未识别",
                                   c.get("process_name") or "—"),
              "- 列→参数命中：**%d / %d 列**" % (c.get("mapped_params"),
                                             c.get("cols")),
              "- 识别到的缺陷：%s" % ("、".join(c.get("defects") or []) or "无"),
              "- 根因候选数：%s" % c.get("rootcause_candidates"),
              "- 结论档位：**%s**" % r["verdict"],
              "- analyze 耗时：%ss" % c.get("elapsed_s"), ""]
        if c.get("notes"):
            L += ["analyze 侧提示："]
            for n in c["notes"]:
                L += ["  - %s" % n]
            L += [""]
        if c.get("unmapped_sample"):
            L += ["未映射列示例：`%s`" % "`、`".join(c["unmapped_sample"][:8]), ""]
        cal = r["caliber"]
        if cal.get("compared"):
            L += ["**口径对照（真实值 vs 知识库 typical_range）**", "",
                  "- 可比参数 %d 个，落在库内 %d 个（%.0f%%）"
                  % (cal["compared"], cal["inside"],
                     100.0 * cal["inside"] / cal["compared"]), ""]
            if cal["outside"]:
                L += ["| 列 | 库内量级 | 真实量级 | n |", "|---|---|---|---|"]
                for o in cal["outside"][:8]:
                    L += ["| %s | %s | %s | %d |"
                          % (o["column"], o["kb_range"], o["data_range"], o["n"])]
                L += ["", "> 落在库外不一定是错——可能是单位不同、也可能是"
                          "知识库量级兜底与实际脱节。两种都要人来判。", ""]
        for w in r["warnings"]:
            L += ["> ⚠ %s" % w, ""]
        if not r["has_truth"]:
            L += ["> 本例无 truth.json：只能验证健壮性/覆盖度/口径，"
                      "**不能**验证判定对错。", ""]
    if path:
        d = os.path.dirname(path)
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        io.open(path, "w", encoding="utf-8", newline="\n").write("\n".join(L) + "\n")
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description="真实数据印证回路（外部数据 → 印证技能）")
    ap.add_argument("--root", default=os.path.join(ROOT_DIR, "realdata"),
                    help="真实数据根目录，默认 realdata/")
    ap.add_argument("--case", help="只跑某个用例")
    ap.add_argument("--report", help="报告输出路径（.md）")
    ap.add_argument("--strict", action="store_true",
                    help="任何用例被拒收即返回退出码 2（给门禁用）")
    a = ap.parse_args(argv)

    if not os.path.isdir(a.root):
        print("真实数据目录不存在：%s" % a.root)
        print("目录约定见 scripts/realdata_check.py 模块头（realdata/<case>/data.csv + source.json）")
        return 2 if a.strict else 0
    cases = ([a.case] if a.case else
             sorted(d for d in os.listdir(a.root)
                    if os.path.isdir(os.path.join(a.root, d))))
    if not cases:
        print("没有可用例（%s 下没有子目录）" % a.root)
        return 0

    reports = []
    for c in cases:
        r = assess(os.path.join(a.root, c))
        reports.append(r)
        flag = "OK  " if r["ok"] else "拒收"
        print("[%s] %-16s %s" % (flag, r["case"],
                                 r["verdict"] if r["ok"] else
                                 (r["problems"][0] if r["problems"] else "")))
        for p in r["problems"][1:]:
            print("        ✗ %s" % p)
        for w in r["warnings"]:
            print("        ⚠ %s" % w[:110])

    txt = render(reports, a.report)
    if a.report:
        print("\n报告：%s" % a.report)
    else:
        print("\n" + txt)

    bad = [r for r in reports if not r["ok"]]
    if a.strict and bad:
        print("\n有 %d 个用例未通过来源/完整性校验" % len(bad))
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""
企业端场景评测：跑完 examples/enterprises/ 下所有场景，抽出关键结论，
与 truth.json 的真值对照，输出命中/未命中清单。

用法：python scripts/eval_enterprise.py
输出：output/eval/summary.md（人读）+ output/eval/results.json（机读）
"""
import os
import io
import sys
import json
import glob
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

OUT = os.path.join(ROOT, "output", "eval")
CASES = os.path.join(ROOT, "examples", "enterprises")

INDUSTRY = {"E01": "battery", "E02": "battery", "E03": "battery",
            "E04": "pv", "E05": "pv", "E06": "pv", "E07": None}


def brief(res):
    """从结果里抽出评测关心的字段"""
    b = {"error": res.get("error"), "detail": str(res.get("detail"))[:200] if res.get("detail") else None}
    b["process"] = res.get("process_name") or res.get("process")
    b["target"] = res.get("target")
    b["rows"] = (res.get("shape") or [None, None])[0]
    b["clean_rows_in"] = (res.get("clean") or {}).get("rows_in")
    rc = res.get("rootcause") or {}
    b["defect"] = rc.get("defect")
    b["root_top"] = [{"name": c.get("param_name"), "score": c.get("score"),
                      "ev": c.get("evidence_score"), "prior": c.get("prior_score"),
                      "status": c.get("data_status")}
                     for c in (rc.get("candidates") or [])[:4]]
    b["corr"] = [{"x": c.get("param"), "r": c.get("r"), "p": c.get("p"), "sig": c.get("significant")}
                 for c in (res.get("association") or [])[:6]]
    b["strong"] = [{"a": s.get("a"), "b": s.get("b"), "r": s.get("r")}
                   for s in (res.get("strong_pairs") or [])[:4]]
    b["cpk"] = []
    for s in (res.get("spc") or []):
        cap = s.get("capability") or {}
        b["cpk"].append({"name": s.get("param_name") or s.get("column"),
                         "cpk": cap.get("Cpk") if cap.get("Cpk") is not None else cap.get("cpk"),
                         "lsl": s.get("lsl"), "usl": s.get("usl"), "src": s.get("spec_source")})
    pt = res.get("pareto")
    b["pareto"] = pt if not isinstance(pt, dict) else (pt.get("rows") or pt.get("items"))
    b["spc_hits"] = [{"col": s.get("column"), "n": len(s.get("violations") or [])}
                     for s in (res.get("spc") or []) if s.get("violations")]
    b["defects"] = res.get("defects")
    b["groups"] = res.get("groups")
    b["insufficient"] = rc.get("evidence_insufficient")
    b["recog"] = res.get("column_recognition_rate")
    b["warns"] = len(res.get("warnings") or [])
    b["gaps"] = [g.get("item") for g in (res.get("gaps") or []) if isinstance(g, dict)][:4]
    return b


def run(cases_dir, out_dir, verbose=True):
    os.makedirs(out_dir, exist_ok=True)
    # 下划线开头的是结果/临时目录，不是用例
    cases = sorted(d for d in os.listdir(cases_dir)
                   if os.path.isdir(os.path.join(cases_dir, d)) and not d.startswith("_"))
    from lib import pipeline as PL
    results = {}
    for cid in cases:
        d = os.path.join(cases_dir, cid)
        truth = json.load(io.open(os.path.join(d, "truth.json"), encoding="utf-8"))
        files = sorted(glob.glob(os.path.join(d, "*.csv")))
        spec_path = os.path.join(d, truth.get("spec_file") or "")
        spec = None
        if os.path.exists(spec_path):
            spec = json.load(io.open(spec_path, encoding="utf-8"))
        ind = INDUSTRY.get(cid[:3])
        outdir = os.path.join(out_dir, cid[:3])
        if verbose:
            print("── %s  %s" % (cid, truth["title"]))
        try:
            res = PL.analyze(files, outdir=outdir, target=None, process=truth.get("process"),
                             spec_overrides=spec, industry=ind)
            b = brief(res)
            b["truth"] = truth
            results[cid] = b
            if not verbose:
                continue
            print("   工序=%s 目标列=%s 缺陷=%s 行数=%s" % (b["process"], b["target"], b["defect"], b["rows"]))
            print("   分组=%s | Pareto=%s" % (b["groups"], "有" if b["pareto"] else "无"))
            print("   根因Top: %s" % ", ".join("%s(%.2f)" % (x["name"], x["score"] or 0) for x in b["root_top"]))
            print("   相关Top: %s" % ", ".join("%s r=%s" % (x["x"], (round(x["r"], 3) if isinstance(x["r"], (int, float)) else x["r"])) for x in b["corr"][:4]))
            print("   Cpk: %s" % ", ".join("%s=%s" % (x["name"], (round(x["cpk"], 2) if isinstance(x["cpk"], (int, float)) else x["cpk"])) for x in b["cpk"][:4]))
            print("   强相关对: %s" % ", ".join("%s~%s(%.2f)" % (s["a"], s["b"], s["r"]) for s in b["strong"][:3]))
            if b["error"]:
                print("   !! error=%s %s" % (b["error"], b["detail"]))
        except Exception as e:
            results[cid] = {"error": "%s: %s" % (type(e).__name__, e), "trace": traceback.format_exc()[-500:], "truth": truth}
            if verbose:
                print("   !! 崩溃 %s: %s" % (type(e).__name__, e))
        if verbose:
            print()

    with io.open(os.path.join(out_dir, "results.json"), "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    _write_summary(results, out_dir)
    if verbose:
        print("结果已写入 %s/results.json 与 summary.md" % out_dir)
    return results


# 每套数据的真因关键词（来自 truth.json 植入的因果链）。
# 命中判据：真因关键词出现在「根因候选前 2 名」或「强相关对」里。
KEY = {
    "E01_锂电涂布": ["烘箱二区", "二区温度"],
    "E02_锂电卷绕": ["卷绕张力", "卷针"],
    "E03_锂电化成": ["搁置时间"],
    "E04_光伏硅片": ["进给速度"],
    "E05_组件EL": ["层压压力"],
    "E06_组件IV": ["焊带电阻", "串联电阻", "Rs"],
    "E07_小厂脏数据": ["__NONE__"],     # 真值：应当承认认不出
}

# 次要真因（双因子场景的第二条）。只抓住主因不算完全命中。
SECOND = {
    "E03_锂电化成": ["CH-07", "通道号", "通道"],
    "E04_光伏硅片": ["供应商"],
    "E06_组件IV": [],
}


def judge(cid, b):
    """对照真值判定单套是否命中。抽出来给 sweep 复用——
    sweep 要靠同一把尺子量几十套变体，判据不能有两份。"""
    keys = KEY.get(cid, [])
    top = (b.get("root_top") or [{}])[0].get("name") or "—"
    strong = " ".join("%s~%s" % (s.get("a"), s.get("b")) for s in (b.get("strong") or []))
    hay = " ".join([top] + [x.get("name", "") for x in (b.get("root_top") or [])] + [strong])
    if "__NONE__" in keys:
        # 判据用「是否明确承认认不出」，而不是分数阈值。
        # 分数随数据浮动，同一份脏数据换种子最高分能在 0.14~0.33 之间跑，
        # 拿阈值判定会把「Agent 其实做对了」误判成失败；而"认不出"这个判断
        # 只取决于列名识别率，是稳定的。
        ok = bool(b.get("insufficient"))
        return ok, ("OK" if ok else "MISS"), (
            "已标记证据不足，未臆造根因" if ok
            else "识别率 %.0f%% 却仍给出根因结论" % ((b.get("recog") or 0) * 100))
    in_root = any(k in " ".join(x.get("name", "") for x in (b.get("root_top") or [])) for k in keys)
    in_clue = any(k in hay for k in keys)
    second_hit = any(k in hay for k in SECOND.get(cid, []))
    if in_root and (second_hit or not SECOND.get(cid)):
        return True, "FULL", ""
    if in_root or in_clue or second_hit:
        miss = []
        if not in_root:
            miss.append("真因未进根因候选")
        if SECOND.get(cid) and not second_hit:
            miss.append("次要真因未检出")
        return False, "PART", "；".join(miss)
    return False, "MISS", "真因完全未出现"


MARK = {"FULL": "✅ 完全", "PART": "⚠️ 部分", "MISS": "❌ 未命中",
        "OK": "✅", "ERR": "💥 崩溃"}


def _write_summary(results, out_dir=None):
    out_dir = out_dir or OUT
    L = ["# 企业端场景评测报告", "",
         "每套数据都植入了已知真值（含一个「看起来像真因其实正常」的陷阱）。",
         "下表对照真值判定命中与否。", "",
         "| 场景 | 真因 | Agent 根因 Top1 | 命中 | 说明 |", "|---|---|---|---|---|"]
    hit_n = 0
    for cid, b in sorted(results.items()):
        t = b.get("truth") or {}
        top = (b.get("root_top") or [{}])[0].get("name") or "—"
        if b.get("error"):
            ok, code, note = False, "ERR", str(b.get("detail") or b.get("error"))[:40]
        else:
            ok, code, note = judge(cid, b)
        mark = MARK.get(code, code)
        if ok:
            hit_n += 1
        L.append("| %s | %s | %s | %s | %s |" % (
            cid, (t.get("primary_cause") or "")[:26], top, mark, note))
    L += ["", "**命中 %d / %d**" % (hit_n, len(results)), ""]
    for cid, b in sorted(results.items()):
        t = b.get("truth") or {}
        L += ["## %s —— %s" % (cid, t.get("title", "")),
              "- 真因：%s" % t.get("primary_cause"),
              "- 机理：%s" % t.get("mechanism"),
              "- 陷阱：%s" % "; ".join(x["name"] + "（" + x["why_not"][:40] + "）"
                                      for x in (t.get("traps") or [])),
              "- 业务价值：%s" % t.get("biz_value"), ""]
    with io.open(os.path.join(out_dir, "summary.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(L))


def sweep(a):
    """
    换 N 个随机种子，各造一批带真值的数据再评测。

    这是「不依赖企业真实数据也能自我完善」的关键：真值由生成器控制，
    换种子就能量产考题。真实数据的价值在于分布更野，但它若不带结论，
    只能验证「不崩」，验证不了「判得对」——所以真实数据不是前提，
    而是对仿真结论的补充校验。
    """
    import importlib.util
    gp = os.path.join(HERE, "gen_enterprise_data.py")
    spec = importlib.util.spec_from_file_location("gen_ent", gp)
    gm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gm)

    root = os.path.join(OUT, "sweep")
    os.makedirs(root, exist_ok=True)
    tally, fails = {}, []

    for i in range(a.sweep):
        seed = a.seed_base + (i + 1) * 7919
        d = os.path.join(root, "s%d" % seed)
        if not os.path.isdir(d) or a.regen:
            gm.main(["--seed", str(seed), "--out", d])
        # 结果目录必须放在用例目录之外，否则会被当成又一个用例来评测
        res = run(d, os.path.join(root, "_eval_s%d" % seed), verbose=False)
        for cid, b in res.items():
            if b.get("error"):
                ok, code, note = False, "ERR", str(b.get("detail") or b.get("error"))[:60]
            else:
                ok, code, note = judge(cid, b)
            tally.setdefault(cid, {"FULL": 0, "PART": 0, "MISS": 0, "ERR": 0,
                                   "OK": 0, "other": 0})
            tally[cid][code] = tally[cid].get(code, 0) + 1
            if code in ("MISS", "ERR", "PART"):
                fails.append({"seed": seed, "cid": cid, "code": code, "note": note})
        print("  第 %d/%d 轮 seed=%d 完成" % (i + 1, a.sweep, seed))

    L = ["# 自我完善 · 多种子回归报告", "",
         "每轮换一个随机种子重造七套带真值的数据，用同一把尺子判定命中。",
         "样本越多，越能暴露「只在特定数据分布下才犯」的缺陷。", "",
         "| 场景 | 完全 | 部分 | 未命中 | 崩溃 | 命中率 |", "|---|---|---|---|---|---|"]
    tot_full = tot = 0
    for cid in sorted(tally):
        c = tally[cid]
        n = sum(c.values())
        good = c.get("FULL", 0) + c.get("OK", 0)
        tot_full += good
        tot += n
        L.append("| %s | %d | %d | %d | %d | %.0f%% |" % (
            cid, c.get("FULL", 0) + c.get("OK", 0), c.get("PART", 0),
            c.get("MISS", 0), c.get("ERR", 0), 100.0 * good / max(n, 1)))
    L += ["", "**总体命中 %d / %d = %.0f%%（%d 个种子 × 7 套场景）**"
          % (tot_full, tot, 100.0 * tot_full / max(tot, 1), a.sweep), ""]
    if fails:
        L += ["## 未完全命中的样本（按场景归并，供定位缺陷）", "",
              "| 场景 | 次数 | 典型判定 |", "|---|---|---|"]
        agg = {}
        for f in fails:
            agg.setdefault(f["cid"], []).append(f)
        for cid in sorted(agg):
            fs = agg[cid]
            L.append("| %s | %d | %s |" % (cid, len(fs), "；".join(
                sorted(set("%s:%s" % (x["code"], x["note"][:24]) for x in fs))[:3])))
        L += ["", "复现单个失败样本：",
              "`python scripts/gen_enterprise_data.py --seed <seed> --out /tmp/x` 后",
              "`python scripts/eval_enterprise.py --dir /tmp/x`。", ""]
    L += ["## 怎么用", "",
          "改了归因/识别逻辑之后跑一遍，命中率不该下降；",
          "出现新的 MISS 就把该 seed 记进回归清单，修完再跑。", "",
          "企业给了真实数据时，把 CSV + truth.json 放进一个目录，",
          "`python scripts/eval_enterprise.py --dir <该目录>` 走同一套判定。"]
    p = os.path.join(root, "sweep.md")
    with io.open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("\nsweep 报告：%s" % p)
    print("总体命中率 %.0f%%（%d/%d）" % (100.0 * tot_full / max(tot, 1), tot_full, tot))
    return tally


def main(argv=None):
    import argparse as _ap
    ap = _ap.ArgumentParser(description="企业端场景评测（对照植入的已知真值）")
    ap.add_argument("--dir", default=CASES, help="待评测的数据目录，默认 examples/enterprises")
    ap.add_argument("--out", default=OUT, help="结果输出目录，默认 output/eval")
    ap.add_argument("--sweep", type=int, default=0,
                    help="换 N 个随机种子各造一批数据再评测（自我完善用）")
    ap.add_argument("--seed-base", type=int, default=20260901, help="sweep 起始种子")
    ap.add_argument("--regen", action="store_true", help="sweep 时强制重造已存在的种子目录")
    a = ap.parse_args(argv)
    if a.sweep:
        sweep(a)
    else:
        run(a.dir, a.out)


if __name__ == "__main__":
    main()

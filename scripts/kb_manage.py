# -*- coding: utf-8 -*-
"""
kb_manage.py —— 工艺知识库持续更新管理台

所有命令均可在 Skill 根目录或任意位置执行（自动定位 knowledge/）。
加 --json 可输出机器可读结果，便于 Agent 解析。

生命周期：
  harvest 候选  ->  propose 入暂存区  ->  list/show 查看
  ->  review --approve 人工放行  ->  sync 合并入正库（自动备份 + 版本自增 + 变更记录）
  ->  export-patch 分发给其它站点  /  import-patch 接收外部补丁（仍进暂存区）
  ->  rollback 回滚到任意一次变更前

常用：
  python scripts/kb_manage.py status
  python scripts/kb_manage.py scaffold --type improvement_case --out case.json
  python scripts/kb_manage.py propose --type param --payload case.json --source '{"site_code":"A基地"}'
  python scripts/kb_manage.py list
  python scripts/kb_manage.py review C20261010-001 --approve --by '工艺工程师'
  python scripts/kb_manage.py sync
  python scripts/kb_manage.py rollback --target specs --backup specs-pre-rollback-20261010-120000.json
"""

import os
import sys
import json
import argparse
import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib import kbstore as KBS  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass


def emit(obj, as_json=False, title=""):
    if as_json:
        print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))
        return
    if title:
        print("=" * 70)
        print(title)
        print("=" * 70)


# ------------------------------------------------------------------ 骨架模板

SCAFFOLDS = {
    "param": {
        "key": "slurry_temp",
        "name": "浆料温度",
        "process": "battery_coating",
        "unit": "℃",
        "role": "control",
        "aliases": ["浆料温度", "slurry temperature", "slurry_temp"],
        "typical_range": [20, 35],
        "control": {"mode": "deviation", "limit": 2},
        "source_tier": "site_validated",
        "note": "补充占位：说明该参数的控制要点、与其它参数的耦合关系、异常时的表现。"
    },
    "param_alias": {
        "key": "solid_content",
        "aliases": ["固含率", "SC"]
    },
    "param_limit": {
        "key": "areal_density",
        "control": {"mode": "deviation_pct", "limit_pct": 1.2, "cv_limit_pct": 0.8},
        "source_tier": "customer_spec",
        "note": "来源：客户受控规格书 XXX 版"
    },
    "process": {
        "key": "battery_drying",
        "name": "极片干燥",
        "domain": "battery",
        "alias": ["干燥", "drying", "烘箱"],
        "key_params": [],
        "common_defects": [],
        "upstream": [], "downstream": []
    },
    "defect_new": {
        "key": "暗斑",
        "process": "battery_coating",
        "likely_params": ["oven_zone1_temp", "moisture_after_dry"],
        "mechanism": "占位：说明物理机理",
        "verify_data": ["占位：需要索取的数据"],
        "quick_checks": ["占位：现场快速确认动作"]
    },
    "defect_map": {
        "defect": "针孔气泡",
        "added_likely_params": ["slurry_temp"],
        "added_quick_checks": ["占位：现场确认动作"],
        "evidence": "占位：本次是通过什么数据/试验得出这条关联的"
    },
    "gap_rule": {
        "level": "P1_重要",
        "items": ["占位：需要向用户索取的数据项"]
    },
    "guide_entry": {
        "title": "占位：判读条目标题",
        "strength": "建议",
        "scope": "占位：适用于哪类数据/工序",
        "rule": "占位：判读动作与阈值。例：当 X 图出现连续 9 点同侧且伴随 Cpk<1.0 时，"
                "先查设备漂移而非原料批次。",
        "pitfall": "占位：常见误判。例：把伴随变量当成根因。"
    },
    "improvement_case": {
        "defect": "针孔气泡",
        "process": "battery_coating",
        "symptom": "占位：现场现象（量化）",
        "root_cause_param": "oven_zone1_temp",
        "related_params": ["coating_speed", "solid_content"],
        "countermeasure": {
            "action": "占位：做了什么（可复制执行）",
            "type": "工艺参数调整",
            "detail": "占位：参数从 A 调到 B",
            "cost": "占位：投入",
            "lead_time_days": 3
        },
        "verification": {
            "method": "PDCA",
            "pdca": {"plan": "", "do": "", "check": "", "act": ""},
            "fmea": {"failure_mode": "", "S": 0, "O": 0, "D": 0,
                     "rpn_before": 0, "S2": 0, "O2": 0, "D2": 0, "rpn_after": 0},
            "result": {"metric": "针孔不良率", "unit": "%",
                       "before": None, "after": None, "sample_size": 0,
                       "period": "2026-xx-xx ~ 2026-xx-xx"}
        },
        "standardization": {"doc_no": "", "control_plan_updated": False, "date": ""}
    },
    "device_profile": {
        "key": "my_tester_2000",
        "name": "某型检测设备 XX-2000",
        "vendor": "厂商名",
        "category": "metrology",
        "processes": ["battery_coating"],
        "export_note": "补充说明该设备导出的注意事项（第几行是表头、有无统计尾行、是否竖表等）",
        "format": {
            "layout": "wide",
            "header_rows": 1,
            "unit_row": False,
            "footer_markers": ["平均", "合计", "Max", "Min", "σ"],
            "skip_row_regex": ["^#"]
        },
        "aliases": {
            "设备私有列名1": "areal_density",
            "设备私有列名2": "coating_speed"
        },
        "key_unit_map": {},
        "unit_map": {},
        "drop_cols": [],
        "signature": {
            "meta_regex": ["XX-2000"],
            "col_any": ["设备私有列名1"],
            "filename_regex": ["^XX2000"]
        }
    },
}

FIELD_HELP = {
    "device_profile": [
        "format.layout：wide=一行一样本；long=竖表（Key-Value，会自动转成宽表）；blocked=多区块",
        "layout 为 long 时必须给 long_key_col 与 long_value_col，否则无法转宽表",
        "signature 至少给一种（meta_regex / col_any / filename_regex），否则永远不会被自动匹配",
        "aliases 里的每个目标参数必须已登记；没有的话先用 --type param 补",
        "单位写在列名括号里（如 厚度(mm)）会自动换算；竖表列名不带单位时用 key_unit_map 补"
    ],
    "improvement_case": [
        "verification.method 必须是 PDCA / PFMEA / DOE / 量产验证 之一",
        "evidence_level 由脚本判定：量化前后对比 + n≥30 + 已标准化 -> A（可直接推荐）",
        "                          流程完整但样本小          -> B（推荐时标注待验证）",
        "                          仅经验描述                -> C（只入库，不参与推荐）",
        "只有顾客最低限度：把 countermeasure.action 写成别人能照着执行的句子",
    ],
    "param": [
        "role=control 表示可调控因子（能进 DOE）；role=response 表示结果指标（不能进 DOE）",
        "typical_range 若非企业受控规格，只作参考，不参与判定",
    ],
}


# ------------------------------------------------------------------ 子命令

def cmd_status(a):
    idx = KBS.load_index()
    specs = KBS._read_json(KBS.SPEC_FILE, {})
    cases = KBS._read_json(KBS.CASE_FILE, {"cases": []})
    interp = KBS._read_json(KBS.INTERP_FILE, {"entries": []})
    pend = KBS.load_candidates("pending")
    appr = KBS.load_candidates("approved")
    lv = {}
    for c in cases.get("cases", []):
        lv[c.get("evidence_level", "?")] = lv.get(c.get("evidence_level", "?"), 0) + 1
    out = {
        "specs": {"version": (specs.get("meta") or {}).get("version"),
                  "revision": (specs.get("meta") or {}).get("revision", 0),
                  "params": len(specs.get("params", {})),
                  "processes": len(specs.get("processes", {})),
                  "defects": len(specs.get("defect_rootcause", {}))},
        "cases": {"version": (cases.get("meta") or {}).get("version"),
                  "total": len(cases.get("cases", [])), "by_level": lv,
                  "recommendable": len([c for c in cases.get("cases", []) if c.get("can_recommend")])},
        "interp": {"version": (interp.get("meta") or {}).get("version"),
                   "entries": len(interp.get("entries", []))},
        "inbox": {"pending": len(pend), "approved": len(appr)},
        "backups": len(KBS.list_backups()),
    }
    emit(out, a.json, "工艺知识库状态")
    if not a.json:
        print("正库 process_specs.json  v%s  (rev %s)" % (out["specs"]["version"], out["specs"]["revision"]))
        print("   参数 %d｜工序 %d｜缺陷机理 %d"
              % (out["specs"]["params"], out["specs"]["processes"], out["specs"]["defects"]))
        print("改善措施库 improvement_library.json  v%s" % out["cases"]["version"])
        print("   收录 %d 条｜可推荐 %d 条｜分级 %s"
              % (out["cases"]["total"], out["cases"]["recommendable"], out["cases"]["by_level"] or "—"))
        print("判读规则 interp_rules.json  v%s｜条目 %d" % (out["interp"]["version"], out["interp"]["entries"]))
        print("暂存区 inbox.jsonl｜待审核 %d｜已放行待合并 %d" % (out["inbox"]["pending"], out["inbox"]["approved"]))
        print("备份 %d 份（knowledge/backups/）" % out["backups"])
        for key, meta in idx.get("assets", {}).items():
            print("   - %-12s %-42s 条目 %-4s v%s" % (key, meta.get("path", "")[:42],
                                                      meta.get("count"), meta.get("version")))


def _slug_key(vendor, model, fallback=None):
    """厂商+型号 → 稳定的档案 key。key 必须稳定，否则下次认不出是同一台设备。"""
    import re as _re
    base = "_".join(x for x in [str(vendor or "").strip(), str(model or "").strip()] if x)
    base = _re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "_", base).strip("_").lower()
    return base or (fallback or "device")


def cmd_devices(a):
    """列出已登记的检测设备档案；给 --file 时顺带认一下这个文件像哪台设备导出的"""
    try:
        from lib import devices as DEV
        from lib import table as T
        from lib import deviceid as DID
        from lib import tenancy as TN
    except Exception as e:
        print("设备档案引擎不可用：%s" % e)
        sys.exit(1)
    import io

    # ── 录入问卷：只问真正缺的
    if getattr(a, "interview", None):
        tb = T.read_any(a.interview)
        if isinstance(tb, list):
            tb = tb[0]
        q = DID.interview(tb, path=a.interview)
        emit(q, a.json, "未知设备录入问卷")
        if not a.json:
            print("文件：%s" % q["file"])
            print("列指纹：%s（%d 列）" % (q["fingerprint"]["col_sig"], q["fingerprint"]["n_cols"]))
            print("已识别为：%s" % (q["identified_as"] or "未识别"))
            if q["identified_as"]:
                print("\n这台设备已在档案库里，无需录入。")
                return
            print("\n已自动填好（可直接改）：")
            for k, v in (q["auto_filled"] or {}).items():
                if v:
                    print("   %-14s %s" % (k, json.dumps(v, ensure_ascii=False)[:110]))
            if q["unmapped_columns"]:
                print("\n未匹配到标准参数的列（%d 个，需要你给出含义）：" % len(q["unmapped_columns"]))
                for c in q["unmapped_columns"][:20]:
                    print("   - %s" % c)
            print("\n还需要问企业的问题：")
            for i, qq in enumerate(q["questions"], 1):
                print("   %d. %s（%s）" % (i, qq["label"], qq["hint"]))
            print("\n把答案填进 JSON（字段名见 questions 的 field）后执行：")
            print("   python scripts/kb_manage.py --tenant <企业id> devices --register 答案.json --key <设备key>")
        return

    # ── 登记为私有设备档案
    if getattr(a, "register", None):
        ans = json.load(io.open(a.register, encoding="utf-8"))
        tb = None
        if ans.get("__file__"):
            try:
                tb = T.read_any(ans["__file__"])
                if isinstance(tb, list):
                    tb = tb[0]
            except Exception:
                tb = None
        prof = DID.build_profile(ans, table=tb, path=ans.get("__file__"))
        if getattr(a, "enrich", None):
            DID.enrich(prof, json.load(io.open(a.enrich, encoding="utf-8")))
        key = a.key or _slug_key(prof.get("vendor"), prof.get("model"), ans.get("key"))
        r = DID.register(prof, key, tenant=getattr(a, "tenant", None), force=a.force)
        if not r.get("ok"):
            print("登记失败：%s" % r.get("error"))
            sys.exit(1)
        doc = DID.save_dossier(key, prof, tenant=getattr(a, "tenant", None))
        r["dossier"] = doc
        emit(r, a.json, "设备档案已登记")
        if not a.json:
            print("设备 key：%s" % key)
            print("档案位置：%s（该企业私有层，共享基线未改动）" % r["path"])
            print("记忆文档：%s" % (doc or "—"))
            print("该企业现有设备档案 %d 台" % r["devices_total"])
            print("\n下次上传这台设备的导出文件会自动认出并套用同一套解析口径。")
        return

    # ── 回填检索结果
    if getattr(a, "enrich", None) and getattr(a, "key", None):
        if not getattr(a, "tenant", None):
            print("回填需要指定企业：--tenant <企业id>")
            sys.exit(1)
        TN.set_tenant(a.tenant)
        path = TN.map_to_tenant(DEV.PROFILE_FILE)
        data = json.load(io.open(path, encoding="utf-8")) if os.path.exists(path) else {"devices": {}}
        if a.key not in (data.get("devices") or {}):
            print("该企业没有设备档案 %s" % a.key)
            sys.exit(1)
        added = DID.enrich(data["devices"][a.key], json.load(io.open(a.enrich, encoding="utf-8")))
        try:
            from lib import hotswap as HS
        except Exception:
            import hotswap as HS                     # noqa
        HS.atomic_write_json(path, data)
        DEV.invalidate_cache()
        doc = DID.save_dossier(a.key, data["devices"][a.key], tenant=a.tenant)
        emit({"key": a.key, "added": added, "dossier": doc}, a.json, "检索结果已回填")
        if not a.json:
            print("新增：别名 %d、单位 %d、规格 %d、出处 %d"
                  % (added["aliases"], added["units"], added["specs"], added["refs"]))
            print("记忆文档：%s" % (doc or "—"))
        return

    # ── 生成记忆文档
    if getattr(a, "dossier", None):
        if not getattr(a, "key", None):
            print("需要 --key")
            sys.exit(1)
        prof = DEV.get_profile(a.key)
        if not prof:
            print("找不到设备档案 %s" % a.key)
            sys.exit(1)
        p = DID.save_dossier(a.key, prof, tenant=getattr(a, "tenant", None))
        emit({"key": a.key, "dossier": p}, a.json, "设备记忆文档")
        if not a.json:
            print("已生成：%s" % p)
        return

    data = DEV.load_profiles()
    devs = data.get("devices", {})

    if getattr(a, "file", None):
        hit = DEV.identify_device(T.read_any(a.file), path=a.file)
        key = hit.get("device")
        info = {"file": a.file, "device": key,
                "name": DEV.get_profile(key).get("name") if key else None,
                "confidence": hit.get("confidence"), "score": hit.get("score"),
                "evidence": hit.get("evidence"), "note": hit.get("note")}
        emit(info, a.json, "导出文件归属判定")
        if not a.json:
            if key:
                print("%s\n  → %s（%s）" % (a.file, info["name"], key))
                print("  置信度 %s｜得分 %.1f｜依据：%s"
                      % (info["confidence"], info["score"] or 0, "、".join(info["evidence"] or []) or "—"))
            else:
                print("%s\n  → 未匹配到已知设备，将按通用版式处理" % a.file)
                print("  %s" % (info["note"] or ""))
        return

    rows = []
    for k, v in devs.items():
        if getattr(a, "process", None) and a.process not in (v.get("processes") or []):
            continue
        fmt = v.get("format") or {}
        rows.append({
            "key": k, "name": v.get("name"), "vendor": v.get("vendor"),
            "layout": fmt.get("layout"), "processes": v.get("processes") or [],
            "aliases": len([x for x in (v.get("aliases") or {}).values() if x]),
            "has_signature": bool(v.get("signature")),
        })
    rows.sort(key=lambda r: (r["vendor"] or "", r["key"]))
    emit({"total": len(rows), "devices": rows,
          "unit_conversions": len(data.get("unit_conversions") or {})},
         a.json, "已登记的检测设备档案")
    if a.json:
        return
    print("共 %d 台（单位换算规则 %d 条），未命中的导出会自动降级为通用版式处理\n"
          % (len(rows), len(data.get("unit_conversions") or {})))
    print("%-26s %-34s %-8s %s" % ("型号/档案 key", "名称", "版式", "别名数 识别特征 关联工序"))
    print("-" * 100)
    for r in rows:
        print("%-26s %-34s %-8s %-5d %-8s %s"
              % (r["key"][:26], (r["name"] or "")[:34], r["layout"] or "-",
                 r["aliases"], "有" if r["has_signature"] else "无", "、".join(r["processes"]) or "—"))
    print("\n登记新设备：scaffold --type device_profile → propose → review --approve → sync")


def cmd_scaffold(a):
    if a.type not in SCAFFOLDS:
        print("未知类型：%s\n可选：%s" % (a.type, ", ".join(sorted(SCAFFOLDS))))
        sys.exit(1)
    tpl = SCAFFOLDS[a.type]
    path = a.out or ("%s模板.json" % a.type)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(tpl, f, ensure_ascii=False, indent=2)
    emit({"path": os.path.abspath(path), "type": a.type,
          "help": FIELD_HELP.get(a.type, [])}, a.json, "已生成骨架")
    if not a.json:
        print("骨架已写入：%s" % os.path.abspath(path))
        for h in FIELD_HELP.get(a.type, []):
            print("   · %s" % h)
        print("填好后执行：")
        print("   python scripts/kb_manage.py propose --type %s --payload \"%s\"" % (a.type, path))


def cmd_propose(a):
    payload = None
    if a.payload:
        with open(a.payload, "r", encoding="utf-8") as f:
            payload = json.load(f)
    elif a.json_str:
        payload = json.loads(a.json_str)
    else:
        print("需要 --payload 文件或 --json 字符串")
        sys.exit(1)
    src, vf = {}, {}
    if a.source:
        src = json.loads(a.source)
    if a.verify:
        vf = json.loads(a.verify)
    rec, err = KBS.add_candidate(a.type, payload, source=src, verification=vf,
                                 proposed_by=a.by or "工艺工程师", note=a.note or "")
    if err:
        emit({"ok": False, "error": err}, a.json)
        print("× %s" % err)
        sys.exit(2)
    warn = rec.get("warnings") or []
    result = {"ok": True, "id": rec["id"], "type": rec["type"],
              "target": rec["target"], "warnings": warn,
              "next": "python scripts/kb_manage.py review %s --approve" % rec["id"]}
    emit(result, a.json, "候选已入暂存区")
    if not a.json:
        print("候选编号：%s  类型：%s  ->  将写入 %s" % (rec["id"], rec["type"], rec["target"]))
        for w in warn:
            print("   ⚠ %s" % w)
        print("下一步： python scripts/kb_manage.py review %s --approve --by '<责任人>'" % rec["id"])


def cmd_list(a):
    recs = KBS.load_candidates(None if a.all else (a.status or "pending"))
    out = [{"id": r.get("id"), "type": r.get("type"), "status": r.get("status"),
            "created": (r.get("created") or "")[:19],
            "by": r.get("proposed_by"),
            "site": (r.get("source") or {}).get("site_code") or (r.get("source") or {}).get("site") or "—",
            "title": _title(r)} for r in recs]
    emit(out, a.json, "候选清单（%s）" % (a.status or "pending"))
    if not a.json:
        if not out:
            print("（空）")
        for r in out:
            print("  %-16s %-16s %-9s %-10s %s" % (r["id"], r["type"], r["status"], r["by"], r["title"]))


def _title(rec):
    pl = rec.get("payload", {})
    for k in ("title", "name", "defect", "key", "symptom"):
        if pl.get(k):
            return str(pl[k])[:34]
    return (pl.get("countermeasure") or {}).get("action", "")[:34] or "—"


def cmd_show(a):
    recs = {r.get("id"): r for r in KBS.load_candidates()}
    r = recs.get(a.id)
    if not r:
        print("候选不存在：%s" % a.id)
        sys.exit(1)
    emit(r, a.json, "候选 %s" % a.id)
    if not a.json:
        print("类型 %s -> %s｜状态 %s｜提议人 %s" % (r["type"], r["target"], r["status"], r.get("proposed_by")))
        if r.get("source"):
            print("来源： %s" % r["source"])
        if r.get("verification"):
            print("验证： %s" % r["verification"])
        for w in (r.get("warnings") or []):
            print("  ⚠ %s" % w)
        print("内容：")
        print(json.dumps(r.get("payload"), ensure_ascii=False, indent=2))
        if a.type_hint:
            for h in FIELD_HELP.get(r["type"], []):
                print("   · %s" % h)


def cmd_review(a):
    if not a.approve and not a.reject:
        print("必须指定 --approve 或 --reject")
        sys.exit(1)
    status = "approved" if a.approve else "rejected"
    rec, err = KBS.update_candidate(a.id, {
        "status": status,
        "review": {"reviewer": a.by or "未署名", "note": a.note or "",
                   "decided_at": datetime.datetime.now().isoformat(timespec="seconds")}})
    if err:
        print("× %s" % err)
        sys.exit(2)
    if status == "rejected":
        emit({"ok": True, "id": a.id, "status": "rejected"}, a.json, "已驳回")
        if not a.json:
            print("已驳回 %s，正库未发生变更。" % a.id)
        return
    warn = rec.get("warnings") or []
    emit({"ok": True, "id": a.id, "status": "approved", "warnings": warn,
          "next": "python scripts/kb_manage.py sync"}, a.json, "已放行")
    if not a.json:
        print("已放行 %s。执行 sync 后生效：" % a.id)
        print("   python scripts/kb_manage.py sync%s" % (" --force" if warn else ""))
        for w in warn:
            print("   ⚠ %s（需 --force 确认）" % w)


def cmd_sync(a):
    r = KBS.sync(ids=a.id, force=a.force, reviewer=a.by or "工艺负责人")
    emit(r, a.json, "知识库同步结果")
    if not a.json:
        print("已合并 %d 条，失败 %d 条" % (len(r["merged"]), len(r["failed"])))
        for L in r["logs"]:
            print("   ✓ %s（-> v%s）" % (L["text"], L["version"]))
        for f in r["failed"]:
            print("   × %s：%s" % (f["id"], f["error"]))
        if r["merged"]:
            print("变更记录追加到 knowledge/CHANGELOG.md；旧版本备份在 knowledge/backups/")
            print("回滚： python scripts/kb_manage.py rollback --target specs "
                  "--backup <备份文件名>（列表见 backups 子命令）")


def cmd_backups(a):
    files = KBS.list_backups(a.target)
    emit(files, a.json, "可用备份")
    if not a.json:
        for f in files:
            print("  %-56s %8d B" % (f["file"], f["size"]))


def cmd_rollback(a):
    ok, msg = KBS.rollback(a.target, a.backup)
    emit({"ok": ok, "message": msg}, a.json)
    if not a.json:
        print(("✓ " if ok else "× ") + msg)
    if not ok:
        sys.exit(2)


def cmd_export(a):
    r = KBS.export_patch(since=a.since, outdir=a.outdir, anonymize=not a.keep_source)
    emit(r, a.json, "已导出知识补丁")
    if not a.json:
        print("补丁文件：%s" % r["path"])
        print("条目数 %d｜%s" % (r["entries"], "已脱敏" if r["anonymized"] else "保留来源信息"))


def cmd_import(a):
    r = KBS.import_patch(a.file, auto_approve=a.auto_approve)
    emit(r, a.json, "补丁导入结果")
    if not a.json:
        print("导入 %d 条候选" % r["imported"])
        for i in r["ids"]:
            print("   + %s" % i)
        for e in r["errors"]:
            print("   × %s" % e)
        if r["ids"]:
            print("注意：外部补丁进入暂存区后仍需 review --approve 才会写入正库。")


def cmd_verify(a):
    """结构完整性自检：引用是否悬空、别名是否冲突、枚举是否越界"""
    KBS.invalidate_cache()
    kb = KBS._read_json(KBS.SPEC_FILE, {})
    params, procs, defects = kb.get("params", {}), kb.get("processes", {}), kb.get("defect_rootcause", {})
    errs, warns = [], []
    alias_owner = {}
    for k, spec in params.items():
        if spec.get("process") and spec["process"] not in procs:
            errs.append("参数 %s 指向不存在的工序 %s" % (k, spec["process"]))
        for al in spec.get("aliases", []) or []:
            n = _norm(al)
            if n in alias_owner and alias_owner[n] != k:
                warns.append("别名「%s」同时归属于 %s 与 %s，匹配结果会不稳定" % (al, alias_owner[n], k))
            alias_owner[n] = k
        ctrl = spec.get("control") or {}
        if ctrl.get("mode") and ctrl["mode"] not in KBS.ALLOWED_MODES:
            errs.append("参数 %s 的 control.mode=%s 非法" % (k, ctrl["mode"]))
        if spec.get("role") not in KBS.ALLOWED_ROLES:
            errs.append("参数 %s 的 role=%s 非法" % (k, spec.get("role")))
    for dk, dv in defects.items():
        if dv.get("process") and dv["process"] not in procs:
            errs.append("缺陷「%s」指向不存在的工序 %s" % (dk, dv["process"]))
        for pk in dv.get("likely_params", []) or []:
            if pk not in params:
                errs.append("缺陷「%s」引用不存在的参数 %s" % (dk, pk))
    for pk, p in procs.items():
        for kp in p.get("key_params", []) or []:
            if kp not in params:
                warns.append("工序 %s 的 key_params 含未知参数 %s" % (pk, kp))
    cases = KBS._read_json(KBS.CASE_FILE, {"cases": []}).get("cases", [])
    seen = set()
    for c in cases:
        cid = c.get("id")
        if cid in seen:
            warns.append("改善措施 id 重复：%s" % cid)
        seen.add(cid)
        if c.get("process") and c["process"] not in procs:
            errs.append("措施 %s 指向不存在的工序 %s" % (cid, c["process"]))
        if c.get("evidence_level") not in ("A", "B", "C"):
            errs.append("措施 %s 的 evidence_level=%s 非法" % (cid, c.get("evidence_level")))
    verify_devices(errs, warns, params, procs)
    out = {"ok": len(errs) == 0, "errors": errs, "warnings": warns,
           "counts": {"params": len(params), "processes": len(procs),
                      "defects": len(defects), "cases": len(cases),
                      "devices": len(KBS.DEV_PROFILES.get("devices", {}))
                      if hasattr(KBS, "DEV_PROFILES") else 0}}
    emit(out, a.json, "知识库完整性校验")
    if not a.json:
        print("参数 %d｜工序 %d｜缺陷 %d｜措施 %d"
              % (len(params), len(procs), len(defects), len(cases)))
        if not errs:
            print("✓ 无结构性错误")
        for e in errs:
            print("  × %s" % e)
        for w in warns:
            print("  ⚠ %s" % w)
    if errs:
        sys.exit(1)


def verify_devices(errs, warns, params, procs):
    """
    校验设备档案库。设备档案可以引用尚未登记的参数 key（本意是允许先描述格式、
    后补参数），但下游会把引用当已存在 —— 历史上这样崩过流水线，所以必须在 CI 里挡住。
    顺便检查：换算法无法换算到目标单位、工序引用不存在、无识别特征（等于死档案）。
    """
    prof = KBS._read_json(KBS.DEV_PROFILE_FILE, {})
    convs = prof.get("unit_conversions", {})
    devices = prof.get("devices", {})
    for dk, dv in devices.items():
        for cname, key in (dv.get("aliases") or {}).items():
            if key and key not in params:
                errs.append("设备 %s 的列名「%s」指向未登记参数 %s" % (dk, cname, key))
        for p in (dv.get("processes") or []):
            if p not in procs:
                errs.append("设备 %s 引用了不存在的工序 %s" % (dk, p))
        convs_ok = set()
        for c in convs.values():
            convs_ok.add((_canon(c.get("from")), _canon(c.get("to"))))
        for colname, m in (dv.get("unit_map") or {}).items():
            pair = (_canon(m.get("read_unit")), _canon(m.get("target_unit")))
            if pair[0] != pair[1] and pair not in convs_ok:
                errs.append("设备 %s 的单位换算 %s: %s→%s 无对应换算规则"
                            % (dk, colname, m.get("read_unit"), m.get("target_unit")))
        for kname, m in (dv.get("key_unit_map") or {}).items():
            pair = (_canon(m.get("read_unit")), _canon(m.get("target_unit")))
            if pair[0] != pair[1] and pair not in convs_ok:
                errs.append("设备 %s 的竖表列单位 %s: %s→%s 无对应换算规则"
                            % (dk, kname, m.get("read_unit"), m.get("target_unit")))
        sig = dv.get("signature") or {}
        if not (sig.get("meta_regex") or sig.get("col_any") or sig.get("filename_regex")):
            warns.append("设备 %s 没有任何识别特征，永远不会被自动匹配到" % dk)
        fmt = dv.get("format") or {}
        _hr = fmt.get("header_rows")
        if _hr not in (None, 1, "auto") and not fmt.get("header_detail_only") \
                and not fmt.get("header_join"):
            warns.append("设备 %s 声明了多级表头但未指定合并方式" % dk)
    # 不同设备的同名列映射到不同参数 —— 允许，但必须人工知晓
    alias_targets = {}
    for dk, dv in devices.items():
        for cname, key in (dv.get("aliases") or {}).items():
            if not key:
                continue
            n = KBS.PP._norm_name(cname)
            if n in alias_targets and alias_targets[n] != key:
                warns.append("列名「%s」在多台设备中指向不同参数（%s / %s），"
                             "已按设备隔离处理，但请确保这不是登记错误"
                             % (cname, alias_targets[n], key))
            alias_targets[n] = key
    return errs, warns


def _canon(u):
    try:
        return KBS.DEV._canon_unit(u)
    except Exception:
        return str(u or "").strip().lower()


def _norm(s):
    import re
    return re.sub(r"[\s_\-/()（）\[\]【】:：.、,，]+\s*$", "", str(s or "")).strip().lower()


def cmd_cases(a):
    cs = KBS.find_cases(defect=a.defect, param_key=a.param, level=a.level,
                        only_recommendable=not a.all)
    out = [{"id": c.get("id"), "defect": c.get("defect"), "level": c.get("evidence_level"),
            "param": c.get("root_cause_param"),
            "action": (c.get("countermeasure") or {}).get("action"),
            "metric": (c.get("verification") or {}).get("result"),
            "doc": (c.get("standardization") or {}).get("doc_no"),
            "source": (c.get("_kbmeta") or {}).get("source")} for c in cs]
    emit(out, a.json, "历史已验证对策")
    if not a.json:
        if not out:
            print("（无匹配记录）")
        for c in out:
            r = c["metric"] or {}
            print("[%s] %s · %s → %s" % (c["level"], c["defect"], c["param"], c["action"]))
            if r.get("before") is not None and r.get("after") is not None:
                ns = r.get("sample_size")
                ns = str(int(ns)) if isinstance(ns, (int, float)) and float(ns).is_integer() \
                    else str(ns or "—")
                print("      %s：%.4g → %.4g %s（n=%s，%s）"
                      % (r.get("metric"), r.get("before"), r.get("after"),
                         r.get("unit") or "", ns, r.get("period") or ""))
            if c.get("doc"):
                print("      标准化文件：%s｜来源：%s" % (c["doc"], c.get("source")))


def cmd_harvest(a):
    """从一次分析产出的 kb_candidates.json 批量生成候选"""
    with open(a.file, "r", encoding="utf-8") as f:
        cands = json.load(f)
    if isinstance(cands, dict):
        cands = cands.get("candidates", [])
    ids, errs = [], []
    for c in cands:
        ctype = c.pop("_type", None) or c.get("_type")
        payload = c.get("payload", c)
        rec, err = KBS.add_candidate(ctype or "param", payload,
                                     source={"site_code": a.site or "本次分析", "origin": a.file},
                                     verification={"method": "", "tier": "inferred"},
                                     proposed_by=a.by or "自动发现",
                                     note=c.get("_reason", ""))
        if err:
            errs.append(err.replace("\n", " "))
        else:
            ids.append(rec["id"])
    emit({"created": ids, "errors": errs}, a.json, "已生成候选")
    if not a.json:
        print("生成候选 %d 条（均由分析自动发现，必须人工补全单位/工序后审核）" % len(ids))
        for i in ids:
            print("   + %s" % i)
        for e in errs:
            print("   × %s" % e)


def cmd_index(a):
    idx = KBS.refresh_index(a.note or "")
    emit(idx, a.json, "知识库清单已刷新")
    if not a.json:
        for key, meta in idx["assets"].items():
            print("  %-12s %-44s 条目 %-5s v%s  %s"
                  % (key, meta.get("path", "")[:44], meta.get("count"),
                     meta.get("version"), meta.get("updated") or ""))


def cmd_update(a):
    """修正候选内容（例如把机器误判的"新参数"改成"别名"）"""
    patch = {}
    if a.payload:
        with open(a.payload, "r", encoding="utf-8") as f:
            patch = {"payload": json.load(f)}
    elif a.json_str:
        patch = {"payload": json.loads(a.json_str)}
    if a.type:
        patch["type"] = a.type
    if a.note:
        patch["note"] = a.note
    if a.by:
        patch["proposed_by"] = a.by
    if not patch:
        print("没有可修改的内容（--payload / --json-str / --type / --note / --by）")
        sys.exit(1)
    rec, err = KBS.update_candidate(a.id, patch, merge_payload=a.merge)
    if err:
        print("× %s" % err)
        sys.exit(2)
    emit({"ok": True, "id": rec["id"], "type": rec["type"],
          "payload": rec["payload"], "warnings": rec.get("warnings", [])},
         a.json, "候选已修正")
    if not a.json:
        print("候选 %s 已更新为 %s" % (rec["id"], rec["type"]))
        for w in (rec.get("warnings") or []):
            print("   ⚠ %s" % w)
        print(json.dumps(rec["payload"], ensure_ascii=False, indent=2))


def cmd_intake(a):
    """把用户手写的 PDCA/PFMEA 表单 Markdown 转成候选 JSON（宽松解析）"""
    with open(a.file, "r", encoding="utf-8") as f:
        md = f.read()
    kv = {}
    for line in md.splitlines():
        m = _re().match(r"^\s*[-*]?\s*([^：:]{2,24})[：:]\s*(.+?)\s*$", line)
        if m:
            kv[m.group(1).strip()] = m.group(2).strip().strip("`")
    payload = {
        "defect": kv.get("缺陷") or kv.get("缺陷类型") or "",
        "process": kv.get("工序") or "",
        "symptom": kv.get("现象") or kv.get("症状") or "",
        "root_cause_param": kv.get("根因参数") or kv.get("参数") or "",
        "countermeasure": {"action": kv.get("改善措施") or kv.get("对策") or "",
                           "type": kv.get("措施类型") or "其他",
                           "detail": kv.get("措施细节") or ""},
        "verification": {
            "method": (kv.get("验证方法") or "PDCA").upper(),
            "pdca": {"plan": kv.get("P") or kv.get("Plan") or "", "do": kv.get("D") or kv.get("Do") or "",
                     "check": kv.get("C") or kv.get("Check") or "", "act": kv.get("A") or kv.get("Act") or ""},
            "fmea": {"failure_mode": kv.get("失效模式") or "",
                     "rpn_before": _num(kv.get("RPN(前)") or kv.get("改善前RPN")),
                     "rpn_after": _num(kv.get("RPN(后)") or kv.get("改善后RPN"))},
            "result": {"metric": kv.get("验证指标") or "", "unit": kv.get("单位") or "",
                       "before": _num(kv.get("改善前")), "after": _num(kv.get("改善后")),
                       "sample_size": _num(kv.get("样本量")) or 0,
                       "period": kv.get("验证周期") or ""},
        },
        "standardization": {"doc_no": kv.get("标准化文件") or kv.get("文件编号") or "",
                            "control_plan_updated": bool(kv.get("控制计划已更新")),
                            "date": kv.get("日期") or ""},
    }
    rec, err = KBS.add_candidate("improvement_case", payload,
                                 source={"site_code": a.site or "现场提交"},
                                 verification=payload["verification"],
                                 proposed_by=a.by or "工艺工程师",
                                 note="由 Markdown 表单导入：" + os.path.basename(a.file))
    if err:
        print("× %s" % err)
        sys.exit(2)
    out = {"ok": True, "id": rec["id"], "payload": payload,
           "level": KBS.grade_case(payload)}
    emit(out, a.json, "已生成改善措施候选")
    if not a.json:
        print("候选编号 %s｜证据级别 %s（%s）"
              % (rec["id"], out["level"][0], "；".join(out["level"][1])))
        print("请核对解析结果，必要时用 update 修正后再 review：")
        print(json.dumps(payload, ensure_ascii=False, indent=2))


def _re():
    import re
    return re


def _num(v):
    try:
        return float(str(v).strip())
    except Exception:
        return None


# ------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description="新能源工艺知识库持续更新管理台")
    p.add_argument("--json", dest="json", action="store_true", help="输出 JSON")
    p.add_argument("--tenant", default=os.environ.get("FDE_TENANT"),
                   help="企业标识：启用后所有读写只作用于该企业私有层")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("status", help="查看知识库状态").set_defaults(func=cmd_status)

    sp = sub.add_parser("scaffold", help="生成候选骨架文件")
    sp.add_argument("--type", required=True)
    sp.add_argument("--out")
    sp.set_defaults(func=cmd_scaffold)

    pp = sub.add_parser("propose", help="提交候选到暂存区")
    pp.add_argument("--type", required=True)
    pp.add_argument("--payload")
    pp.add_argument("--json-str", dest="json_str")
    pp.add_argument("--source", help='JSON，如 \'{"site_code":"A基地"}\'')
    pp.add_argument("--verify", help='JSON，如 \'{"method":"PDCA"}\'')
    pp.add_argument("--by", default="工艺工程师")
    pp.add_argument("--note", default="")
    pp.set_defaults(func=cmd_propose)

    lp = sub.add_parser("list", help="列出候选")
    lp.add_argument("--status", default="pending")
    lp.add_argument("--all", action="store_true")
    lp.set_defaults(func=cmd_list)

    shp = sub.add_parser("show", help="查看候选详情")
    shp.add_argument("id")
    shp.add_argument("--type-hint", dest="type_hint", action="store_true")
    shp.set_defaults(func=cmd_show)

    rp = sub.add_parser("review", help="审核：放行或驳回")
    rp.add_argument("id")
    rp.add_argument("--approve", action="store_true")
    rp.add_argument("--reject", action="store_true")
    rp.add_argument("--by", default="")
    rp.add_argument("--note", default="")
    rp.set_defaults(func=cmd_review)

    yp = sub.add_parser("sync", help="把已放行候选合并进正库")
    yp.add_argument("--id", action="append")
    yp.add_argument("--force", action="store_true")
    yp.add_argument("--by", default="工艺负责人")
    yp.set_defaults(func=cmd_sync)

    bp = sub.add_parser("backups", help="列出备份")
    bp.add_argument("--target")
    bp.set_defaults(func=cmd_backups)

    rbp = sub.add_parser("rollback", help="回滚到指定备份")
    rbp.add_argument("--target", required=True, choices=["specs", "cases", "interp"])
    rbp.add_argument("--backup", required=True)
    rbp.set_defaults(func=cmd_rollback)

    ep = sub.add_parser("export-patch", help="导出可分发的知识补丁")
    ep.add_argument("--since")
    ep.add_argument("--outdir")
    ep.add_argument("--keep-source", action="store_true", help="保留客户来源信息（默认脱敏）")
    ep.set_defaults(func=cmd_export)

    ip = sub.add_parser("import-patch", help="导入外部补丁（进暂存区）")
    ip.add_argument("file")
    ip.add_argument("--auto-approve", action="store_true")
    ip.set_defaults(func=cmd_import)

    sub.add_parser("verify", help="知识库完整性校验").set_defaults(func=cmd_verify)

    cp = sub.add_parser("cases", help="查询历史已验证对策")
    cp.add_argument("--defect")
    cp.add_argument("--param")
    cp.add_argument("--level", choices=["A", "B", "C"])
    cp.add_argument("--all", action="store_true", help="含 C 级（不推荐等级）")
    cp.set_defaults(func=cmd_cases)

    hp = sub.add_parser("harvest", help="从分析产出的 kb_candidates.json 生成候选")
    hp.add_argument("--file", required=True)
    hp.add_argument("--site")
    hp.add_argument("--by")
    hp.set_defaults(func=cmd_harvest)

    xp = sub.add_parser("index", help="刷新知识库清单")
    xp.add_argument("--note", default="")
    xp.set_defaults(func=cmd_index)

    up = sub.add_parser("update", help="修正候选内容与类型")
    up.add_argument("id")
    up.add_argument("--payload")
    up.add_argument("--json-str", dest="json_str")
    up.add_argument("--type", help="改判类型，如把 param 改判为 param_alias")
    up.add_argument("--merge", action="store_true", help="与原 payload 合并而非整体替换")
    up.add_argument("--note", default="")
    up.add_argument("--by", default="")
    up.set_defaults(func=cmd_update)

    tp = sub.add_parser("intake", help="把 PDCA/PFMEA 表单 Markdown 转成候选")
    tp.add_argument("--file", required=True)
    tp.add_argument("--site")
    tp.add_argument("--by")
    tp.set_defaults(func=cmd_intake)

    dp = sub.add_parser("devices", help="列出已登记的检测设备档案 / 录入未知设备")
    dp.add_argument("--process", help="只看某工序的设备，如 battery_coating")
    dp.add_argument("--file", help="顺便认一下这个导出文件属于哪台设备")
    dp.add_argument("--interview", metavar="FILE",
                    help="未知设备录入问卷：自动推断能推断的，只列出真正需要问的")
    dp.add_argument("--register", metavar="PAYLOAD.json",
                    help="把填好的问卷登记为该企业的私有设备档案（需 --tenant，也可加 --key）")
    dp.add_argument("--key", help="设备档案 key，如 coater_line3；不给则按厂商+型号自动生成")
    dp.add_argument("--enrich", metavar="FINDINGS.json",
                    help="把检索到的参数标准回填进档案：只补空缺，不覆盖已确认内容")
    dp.add_argument("--dossier", action="store_true",
                    help="为指定 --key 的设备生成/更新设备记忆文档 md")
    dp.add_argument("--force", action="store_true", help="覆盖同名设备档案")
    dp.set_defaults(func=cmd_devices)

    np_ = sub.add_parser("tenants", help="多企业管理：列出 / 清空某个企业的私有层")
    np_.add_argument("--reset", help="清空指定企业的私有层（回到基线）")
    np_.add_argument("--promote", action="store_true", help="列出待提升到基线的投递")
    np_.add_argument("--industries", nargs="+",
                     help="给该企业配置行业模块，如 --industries battery pv（留空表示不限行业）")
    np_.set_defaults(func=cmd_tenants)

    ip = sub.add_parser("industries", help="行业作用域：列出可用行业 / 查看某行业的检索范围")
    ip.add_argument("--show", help="查看某行业包含哪些工序与参数量，如 battery")
    ip.set_defaults(func=cmd_industries)

    up = sub.add_parser("upgrade", help="基线升级预检：有什么可更新、需不需要人工审核")
    up.add_argument("--apply", action="store_true",
                    help="确实要合并已审核通过的候选（默认只体检不动库）")
    up.set_defaults(func=cmd_upgrade)

    a = p.parse_args()
    if getattr(a, "tenant", None):
        try:
            from lib import tenancy as TN
        except Exception:
            import tenancy as TN                     # noqa
        TN.set_tenant(a.tenant)
        try:
            from lib import preprocess as PP
            PP._KNOWLEDGE = None
        except Exception:
            pass
    if not getattr(a, "func", None):
        p.print_help()
        sys.exit(0)
    a.func(a)


def cmd_upgrade(a):
    """
    基线升级预检。

    刻意不做"定时自动入库"：知识库条目是要拿去做工艺判定的，
    机器提的候选必须过人。定时任务跑这里只负责**告诉你有什么待办**，
    真正的合并要显式 --apply（且仍然只合并 approved 的）。
    """
    try:
        from lib import tenancy as TN
    except Exception:
        import tenancy as TN                         # noqa
    if TN.current():
        print("当前在企业「%s」的私有层下，基线预检请不带 --tenant 运行" % TN.current())
        return
    pend = KBS.load_candidates("pending")
    appr = KBS.load_candidates("approved")
    promo = TN.list_promotions("pending")
    specs = KBS._read_json(KBS.SPEC_FILE, {})
    meta = specs.get("meta") or {}
    idx = KBS.load_index() or {}
    print("基线当前状态")
    print("  正库 v%s（rev %s）｜参数 %d｜工序 %d｜缺陷机理 %d"
          % (meta.get("version"), meta.get("revision", 0),
             len(specs.get("params", {})), len(specs.get("processes", {})),
             len(specs.get("defect_rootcause", {}))))
    print("  最近更新：%s" % (idx.get("updated") or meta.get("updated") or "未知"))
    print("\n可更新项")
    print("  待审核候选   %d 条" % len(pend))
    print("  已审核待入库 %d 条" % len(appr))
    print("  企业投递待采纳 %d 条" % len(promo))
    for r in pend[:5]:
        print("    · [%s] %s —— %s" % (r.get("type"), r.get("id"),
                                       (r.get("payload") or {}).get("name")
                                       or (r.get("payload") or {}).get("title")
                                       or (r.get("note") or "")[:40]))
    if not (pend or appr or promo):
        print("\n  → 无任何待办，本次无需更新。定时任务到这里就该结束，不要空跑 sync。")
        return
    if not a.apply:
        print("\n本命令默认只体检。确认无误后加 --apply 才会合并**已审核**的候选。")
        return
    if not appr:
        print("\n没有已审核通过的候选可合并。请先：")
        print("  python scripts/kb_manage.py list            # 看候选")
        print("  python scripts/kb_manage.py review --id <候选id> --approve")
        return
    res = KBS.sync(reviewer=getattr(a, "by", None) or "基线维护")
    emit(res, getattr(a, "json", False), "基线升级完成")
    if not getattr(a, "json", False):
        print("已合并 %d 条：%s" % (len(res.get("merged", [])),
                                    "、".join(res.get("merged", []))))
        for f in res.get("failed", []):
            print("  × %s" % f.get("error"))


def cmd_industries(a):
    """行业作用域：可用行业清单，以及某个行业到底能检索到什么"""
    try:
        from lib import scope as SC, preprocess as PP
    except Exception:
        import scope as SC                           # noqa
        import preprocess as PP                      # noqa
    reg = SC.registry().get("industries") or {}
    if not reg:
        print("尚未登记任何行业（检查 knowledge/industries.json）")
        return
    if a.show:
        valid, bad = SC.resolve(a.show)
        if not valid:
            print("未识别的行业：%s\n可用：%s" % (a.show, "、".join(sorted(reg))))
            return
        SC.set_industries(valid)
        PP._KNOWLEDGE = None
        PP._KNOWLEDGE_SCOPE = "__unset__"
        kb = PP.load_knowledge()
        st = SC.last_stat()
        print("行业「%s」(%s) 检索范围：" % (reg.get(valid[0], {}).get("name", valid[0]),
                                            valid[0]))
        print("  工序 %d 个：%s" % (len(kb["processes"]),
                                    "、".join(sorted(kb["processes"]))))
        print("  参数 %d 条（排除其它行业 %d 条）"
              % (st.get("kept_params", 0), st.get("dropped_params", 0)))
        print("  缺陷机理 %d 条" % len(kb.get("defect_rootcause", {})))
        SC.clear()
        PP._KNOWLEDGE = None
        PP._KNOWLEDGE_SCOPE = "__unset__"
        return
    print("可用行业 %d 个：" % len(reg))
    for k, v in sorted(reg.items()):
        print("  %-10s %-8s 工序 %d 个%s"
              % (k, v.get("name", ""), len(v.get("processes") or []),
                 (" —— " + v["note"]) if v.get("note") else ""))
    print("\n用法：")
    print("  给企业配置：python scripts/kb_manage.py --tenant A tenants --industries battery")
    print("  单次分析指定：python -m lib.pipeline -i data.csv --industry battery")
    print("  注：--tenant 是顶层参数，必须写在子命令前面")


def cmd_tenants(a):
    try:
        from lib import tenancy as TN
    except Exception:
        import tenancy as TN                         # noqa
    if getattr(a, "industries", None) is not None:
        try:
            from lib import scope as SC
        except Exception:
            import scope as SC                       # noqa
        if not TN.current():
            print("请先用 --tenant 指定企业，再配置行业")
            return
        valid, bad = SC.resolve(a.industries)
        if bad:
            print("忽略无法识别的行业：%s（可用：%s）"
                  % ("、".join(bad), "、".join(SC.known_industries())))
        cfg = TN.write_config({"industries": valid})
        print("企业「%s」行业作用域 = %s"
              % (TN.current(), "、".join(valid) if valid else "不限行业（全行业）"))
        return
    if a.reset:
        ok = TN.reset_tenant(a.reset)
        print(("已清空企业「%s」的私有层，其分析将回落到共享基线"
               % a.reset) if ok else "未找到该企业：%s" % a.reset)
        return
    if a.promote:
        pend = TN.list_promotions("pending")
        if not pend:
            # 如实说明，而不是只说"暂无"：
            # 「贡献回共享基线」的投递通道（tenancy.queue_promotion）目前没有任何调用者，
            # 对外承诺只到「沉淀入库」（sync 子命令）。所以队列恒为空是设计现状，
            # 不是数据丢了——不说清楚，使用者会当成丢数据去排查。
            print("暂无待提升的知识投递。")
            print("  说明：投递入口 tenancy.queue_promotion 尚未接入任何流程，")
            print("        当前对外承诺只到「沉淀入库」（sync 子命令）。")
            print("        队列恒为空是设计现状，不是数据丢失。")
            return
        print("待提升投递 %d 条：" % len(pend))
        for r in pend:
            print("  [%s] %s 来自 %s —— %s" % (r.get("kind"), r.get("ts"),
                                              r.get("tenant"), (r.get("note") or "")[:50]))
        return
    ts = TN.list_tenants()
    if not ts:
        print("当前没有任何企业私有层（所有人都在用共享基线）")
        return
    print("已登记企业 %d 家：" % len(ts))
    for t in ts:
        kb = os.path.join(TN.root(), "tenants", t, "knowledge")
        n = 0
        sp = os.path.join(kb, "process_specs.json")
        if os.path.exists(sp):
            try:
                n = len(json.load(io.open(sp, encoding="utf-8")).get("params", {}))
            except Exception:
                n = -1
        ind = TN.read_config(t).get("industries") or []
        print("  %-24s 私有参数 %-5s 行业 %s"
              % (t, n if n >= 0 else "解析失败",
                 "、".join(ind) if ind else "不限（全行业）"))
    print("\n基线目录：%s（只读共享，任何企业的写入都不会落到这里）"
          % os.path.join(TN.root(), "knowledge"))


if __name__ == "__main__":
    main()

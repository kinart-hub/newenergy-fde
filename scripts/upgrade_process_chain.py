# -*- coding: utf-8 -*-
"""
把工序的上下游关系补成机器可遍历的链路，并给每个缺陷登记"上游带入嫌疑"。

背景：原 upstream/downstream 是中文名字符串（如"浆料制备""分切""层压"），
绝大多数在我们的工序表里没有对应 key，机器读不懂，跨工序追溯无从下手。

用法： python scripts/upgrade_process_chain.py
会先备份 knowledge/process_specs.json 到 knowledge/backups/
"""
import os
import sys
import json
import io
import datetime
import shutil

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC = os.path.join(ROOT, "knowledge", "process_specs.json")
BACKUP = os.path.join(ROOT, "knowledge", "backups")

CHAIN = {
    "battery_coating":     {"upstream_keys": [], "downstream_keys": ["battery_calendering"]},
    "battery_calendering": {"upstream_keys": ["battery_coating"], "downstream_keys": ["battery_winding"]},
    "battery_winding":     {"upstream_keys": ["battery_calendering"], "downstream_keys": ["battery_filling"]},
    "battery_filling":     {"upstream_keys": ["battery_winding"], "downstream_keys": ["battery_formation"]},
    "battery_formation":   {"upstream_keys": ["battery_filling"], "downstream_keys": []},
    "pv_wafer":            {"upstream_keys": [], "downstream_keys": ["pv_cell_el"]},
    "pv_cell_el":          {"upstream_keys": ["pv_wafer"], "downstream_keys": ["pv_module_iv"]},
    "pv_module_iv":        {"upstream_keys": ["pv_cell_el"], "downstream_keys": []},
}

# carry_over_from: 这个缺陷有可能是从哪些上游工序"带进来"的（不是本工序自己产生的）
# clearing_evidence: 要排除带入嫌疑，必须拿到的上游证据
# 判断依据是每条缺陷已有的 mechanism，不做凭空臆造；标注为"嫌疑"，最终仍以数据为准
CARRY = {
    "面密度超差": {
        "carry_over_from": [],
        "clearing_evidence": ["本工序为整线该指标的源头工序，追溯目标指向浆料制备（未在本库中建模）"],
    },
    "针孔气泡": {
        "carry_over_from": [],
        "clearing_evidence": ["浆料脱泡记录与静置时间（浆料工序未在本库中建模，需另行索取）"],
    },
    "厚边": {
        "carry_over_from": [],
        "clearing_evidence": ["浆料粘度批次检验记录"],
    },
    "极片错位": {
        "carry_over_from": ["battery_calendering"],
        "clearing_evidence": ["上游极片厚度一致性（同批次压后厚度分布）",
                              "上游是否有波浪边/厚度不均的不良记录"],
    },
    "隔膜褶皱": {
        "carry_over_from": [],
        "clearing_evidence": ["隔膜来料批次与放卷张力设定"],
    },
    "内短路": {
        "carry_over_from": [],
        "clearing_evidence": ["分切毛刺抽检记录（分切工序未在本库中建模，需另行索取）"],
    },
    "自放电超标": {
        "carry_over_from": ["battery_winding", "battery_filling"],
        "clearing_evidence": ["上游卷绕的毛刺/异物检出记录",
                              "上游注液的极片含水量与露点记录",
                              "上游内短路检出率（同批次电芯）"],
    },
    "容量一致性差": {
        "carry_over_from": ["battery_coating", "battery_filling"],
        "clearing_evidence": ["上游涂布面密度的批次均值与 Cpk（容量对涂布量最敏感）",
                              "上游注液量偏差的分布",
                              "化成柜各温区的实际温度分布"],
    },
    "内阻偏高": {
        "carry_over_from": ["battery_calendering", "battery_filling"],
        "clearing_evidence": ["上游辊压的压实密度与是否过压",
                              "上游注液后的浸润时间与浸润效果",
                              "焊接工序接触电阻（未在本库中建模）"],
    },
    "TTV超差": {
        "carry_over_from": [],
        "clearing_evidence": ["本工序为硅片几何量的源头；再往上需索取切片线锯参数（未在本库中建模）"],
    },
    "隐裂": {
        "carry_over_from": ["pv_wafer"],
        "clearing_evidence": ["上游硅片的翘曲度 Warp 与厚度分布",
                              "来料批次与供应商是否变更",
                              "EL 隐裂是否集中在某一来料批次"],
    },
    "断栅": {
        "carry_over_from": ["pv_wafer"],
        "clearing_evidence": ["上游硅片厚度一致性与TTV",
                              "印刷浆料粘度批次记录"],
    },
    "FF偏低": {
        "carry_over_from": ["pv_cell_el"],
        "clearing_evidence": ["上游 EL 检出的隐裂/断栅比例",
                              "串联电阻 Rs 的分档分布"],
    },
    "热斑": {
        "carry_over_from": ["pv_cell_el"],
        "clearing_evidence": ["上游是否有功率失配分拣",
                              "组件内电池片的电流分档一致性"],
    },
    "功率衰减": {
        "carry_over_from": ["pv_cell_el"],
        "clearing_evidence": ["上游电池片的初始效率与衰减曲线（LeTID/PID）",
                              "封装材料批次"],
    },
}


def main():
    with io.open(SPEC, "r", encoding="utf-8") as f:
        data = json.load(f)

    processes = data.get("processes", {})
    missing = [k for k in CHAIN if k not in processes]
    if missing:
        print("工序不存在：%s" % missing)
        sys.exit(1)

    os.makedirs(BACKUP, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:22]
    shutil.copy2(SPEC, os.path.join(BACKUP, "specs-pre-chain-%s.json" % stamp))

    n_p, confirmed = 0, 0
    for key, link in CHAIN.items():
        node = processes[key]
        # 中文名部位保留给人类阅读；*_keys 才是机器遍历用的
        node["upstream_keys"] = link["upstream_keys"]
        node["downstream_keys"] = link["downstream_keys"]
        # 反向一致性：本工序的上游，其 downstream 里也应当有我
        for up in link["upstream_keys"]:
            if key not in processes.get(up, {}).get("downstream_keys", []):
                processes[up]["downstream_keys"].append(key)
        n_p += 1

    # 链建好后回填反向关系，保证 A.downstream 与 B.upstream 互为对称
    for key, node in processes.items():
        for dn in node.get("downstream_keys", []):
            if key not in (processes.get(dn, {}).get("upstream_keys") or []):
                processes[dn]["upstream_keys"].append(key)

    defects = data.get("defect_rootcause", {})
    unknown, n_d = [], 0
    for dname, extra in CARRY.items():
        node = defects.get(dname)
        if node is None:
            unknown.append(dname)
            continue
        proc = node.get("process")
        ok = [p for p in extra["carry_over_from"]
              if p in (processes.get(proc, {}).get("upstream_keys") or [])
              or _is_upstream(processes, proc, p)]
        if len(ok) != len(extra["carry_over_from"]):
            print("  ! %s 的 carry_over_from 里有工序不在上游链上，已剔除：%s → %s"
                  % (dname, extra["carry_over_from"], ok))
        node["carry_over_from"] = ok
        node["clearing_evidence"] = extra["clearing_evidence"]
        n_d += 1
    if unknown:
        print("! 未找到缺陷条目：%s" % unknown)

    meta = data.setdefault("meta", {})
    meta["version"] = _bump(meta.get("version", "1.0.0"))
    meta["updated"] = datetime.date.today().isoformat()
    meta["revision"] = int(meta.get("revision", 0)) + 1
    meta["chain_note"] = ("upstream_keys/downstream_keys 是机器遍历用的工序链（原始 upstream/downstream "
                          "是中文名，保留给人读）。defect.carry_over_from 表示本缺陷有可能是从哪些上游工序"
                          "带入的嫌疑清单，clearing_evidence 是排除嫌疑所需的证据。均为嫌疑与线索，"
                          "最终判定必须以实测数据为准。")

    with io.open(SPEC, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print("已更新 %d 个工序的链路、%d 条缺陷的上游带入嫌疑" % (n_p, n_d))
    print("版本 %s｜备份 specs-pre-chain-%s.json" % (meta["version"], stamp))


def _is_upstream(processes, proc, candidate):
    """沿 upstream_keys 往上走最多 4 跳，看 candidate 是否在链上"""
    seen, stack, hops = set(), [proc], 0
    while stack and hops < 4:
        cur = stack.pop()
        for up in (processes.get(cur, {}).get("upstream_keys") or []):
            if up == candidate:
                return True
            if up not in seen:
                seen.add(up)
                stack.append(up)
        hops += 1
    return False


def _bump(ver, kind="minor"):
    try:
        a, b, c = [int(x) for x in str(ver).split(".")]
    except Exception:
        return "1.1.0"
    return "%d.%d.%d" % (a, b + 1, 0)


if __name__ == "__main__":
    main()

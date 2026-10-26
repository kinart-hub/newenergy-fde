# -*- coding: utf-8 -*-
"""P1-4 数据契约（Data Contract）。

为什么需要它：外部审计报告点名「输入—清洗—分析—报告各环节缺字段级契约」。
实际代价已经付过一次了：provenance 把输入摘要的字节数字段从 size 改成 bytes，
报告渲染那一处还在读 size，当场 KeyError——**同一个概念两套键名**，
而这个错误是在给客户出报告时才炸的，不是跑不出数。

契约要解决的是三件事：
  1. 每个字段说清是什么：类型、单位、能否为空、合理区间、目标方向、从哪来
  2. 版本不兼容要喊：新增字段可以（向前兼容），改语义或删字段必须报
  3. 未知字段要隔离而不是静默接受：没登记的列不能悄悄混进结论，
     它应当被点名挂到 contract_quarantine 上，让报告写明「这一列没进判定」

设计上刻意不做成重型框架：只有规格表 + 两个校验入口 + 一个隔离区。
做重了没人用，没人用的契约等于没有契约。
"""

from __future__ import unicode_literals

import io
import json
import math
import os

SCHEMA_VERSION = "1.0"

# 语义化版本：主版本变了表示**不兼容改动**（改语义/删字段），次版本变了表示新增可选字段。
# 判据是「能不能被老消费者安全读懂」，不是“有没有改动”。


def _parse_ver(v):
    """把 "1.0" 解析成 (1, 0)。解析不出来返回 None——不猜，让调用方去处理。"""
    try:
        parts = [int(x) for x in str(v).strip().split(".")]
    except (ValueError, AttributeError):
        return None
    while len(parts) < 2:
        parts.append(0)
    return (parts[0], parts[1])


def compatible(seen, expect=SCHEMA_VERSION):
    """版本兼容性判定。

    返回 (ok, level, msg)；level 取值 ok / warn / block。
    「不认识的号」按 block 处理：宁可让人补一行登记，也不要把 unknown 当成能用。
    """
    a, b = _parse_ver(seen), _parse_ver(expect)
    if a is None:
        return False, "block", "无法解析的契约版本号：%r" % (seen,)
    if a[0] != b[0]:
        return False, "block", ("主版本不同 (%s vs %s)，属不兼容改动，"
                                "旧数据不能直接喂给新版分析逻辑" % (seen, expect))
    if a[1] > b[1]:
        return True, "warn", ("数据带的次版本 (%s) 比当前实现 (%s) 新，"
                              "可能含本版本尚不认识的字段，未知字段会被隔离" % (seen, expect))
    return True, "ok", "版本兼容"


def field(name, type_="number", unit=None, required=False, nullable=True,
          min_value=None, max_value=None, direction=None, source=None,
          desc=None):
    """一条字段规格。

    direction 是这里唯一必须显式登记的语义：**目标方向没登记清楚，
    后面的"最差档"判定就只能在猜**（这个坑踩过：把越大约好判成越小越好，
    报告建议把工程师往错的反方向调）。所以不填不是"默认中性"，是"来源未明"。
    """
    return {
        "name": name,
        "type": type_,              # number / text / enum / bool / list / object / any
        "unit": unit,
        "required": bool(required),
        "nullable": bool(nullable),
        "min": min_value,
        "max": max_value,
        "direction": direction,     # higher_is_better / lower_is_better /
                                    # higher_is_worse / lower_is_worse /
                                    # target_is_best / two_sided_spec / None(未登记)
        "source": source,           # 该取值的出处。来源不清就不敢用于对外结论
        "desc": desc,
    }


# ---------------------------------------------------------------- 输入表契约
#
# 只登记「跨工序通用」的角色字段。某工序该有哪些参数列，由知识库的
# process_specs 决定，不在这里写死——写死就意味着换个行业要改代码，
# 而这个项目的第一条纪律就是「流程行业无关」。
INPUT_SCHEMA = {
    "version": SCHEMA_VERSION,
    "fields": [
        field("批次号", type_="text", required=False, desc="分批追溯的主键"),
        field("设备号", type_="text", required=False, desc="横向排查用的机台标识"),
        field("班次", type_="text", required=False, desc="班组/轮班，常用于混杂检查"),
        field("时间", type_="any", required=False, desc="时间戳，格式随设备而定"),
    ],
    "roles": ["批次号", "设备号", "班次", "时间"],   # 允许出现、且不会进工艺判定的列
}


def _is_number(v):
    if isinstance(v, bool):
        return False
    if isinstance(v, (int, float)):
        return not (isinstance(v, float) and (math.isnan(v) or math.isinf(v)))
    return False


def check_values(table, fields):
    """按字段契约逐列体检：类型、可空、允许区间。

    这一层是 P1-4 里最容易漏的半边——只登记"这列叫什么"不够，
    还得回答"它的取值合不合规"。实测价值在于：设备偶发的
    -9999/9999 之类的哨兵值、单位没换导致的 1000 倍偏差、
    必填列整列为空，都能在进分析之前被点名，而不是到最后
    变成一条谁也解释不了的"某参数相关性极强"。
    """
    out = {"ok": True, "checked": 0, "violations": [], "by_column": {}}
    try:
        cols = list(table.columns or [])
    except Exception as exc:
        out["ok"] = False
        out["violations"].append("读不到表结构：%s" % exc)
        return out
    low = {str(c).strip().lower(): c for c in cols}

    for spec in fields or []:
        if not isinstance(spec, dict) or not spec.get("name"):
            continue
        name = spec["name"]
        real = low.get(str(name).strip().lower())
        if real is None:
            if spec.get("required"):
                out["violations"].append("必填列「%s」整列缺失" % name)
                out["by_column"][name] = {"missing": True}
            continue                      # 非必填且不存在：不报，交给 check_input 的未知列逻辑
        try:
            vals = list(table.column(real) or [])
        except Exception as exc:
            out["violations"].append("列「%s」读取失败：%s" % (name, exc))
            continue
        out["checked"] += 1
        rec = {"n": len(vals), "issues": []}

        blanks = [v for v in vals
                  if v is None or (isinstance(v, str) and not v.strip())]
        if blanks and not spec.get("nullable", True):
            rec["issues"].append("有 %d 个空值，但契约要求不可为空" % len(blanks))
            out["violations"].append("列「%s」%s" % (name, rec["issues"][-1]))

        t = (spec.get("type") or "any").lower()
        if t == "number":
            nums, nonnum = [], []
            for v in vals:
                if v is None or (isinstance(v, str) and not v.strip()):
                    continue
                if _is_number(v):
                    nums.append(float(v))
                else:
                    nonnum.append(v)
            if nonnum:
                rec["issues"].append("含 %d 个非数值（如 %r）" % (len(nonnum), nonnum[0]))
                out["violations"].append("列「%s」%s" % (name, rec["issues"][-1]))
            lo, hi = spec.get("min"), spec.get("max")
            if nums and lo is not None:
                bad = [x for x in nums if x < float(lo)]
                if bad:
                    rec["issues"].append("%d 个值低于下限 %s（最小 %g）"
                                         % (len(bad), lo, min(bad)))
                    out["violations"].append("列「%s」%s" % (name, rec["issues"][-1]))
            if nums and hi is not None:
                bad = [x for x in nums if x > float(hi)]
                if bad:
                    rec["issues"].append("%d 个值高于上限 %s（最大 %g）"
                                         % (len(bad), hi, max(bad)))
                    out["violations"].append("列「%s」%s" % (name, rec["issues"][-1]))
        out["by_column"][name] = rec
        if rec["issues"]:
            out["ok"] = False
    return out


def check_input(table, known_columns=None, role_columns=None, fields=None):
    """校验输入表，返回 {"ok","version","version_note","unknown","violations","roles"}

    这里的重点不是"卡住不让跑"，而是**把没登记的列点名出来**：
    「不知道这列是什么」和「知道它是日期所以不参与工艺判定」是两回事，
    前者必须暴露给使用者，否则报告看上去完整，实际少了几列的证据。
    """
    out = {
        "ok": True, "version": SCHEMA_VERSION, "version_note": "",
        "unknown": [], "violations": [], "roles": [],
        "n_columns": 0, "n_rows": 0,
    }
    try:
        cols = list(table.columns or [])
    except Exception as exc:
        out["ok"] = False
        out["violations"].append("读不到表结构：%s" % exc)
        return out
    try:
        out["n_rows"] = len(table)
    except Exception:
        out["n_rows"] = 0
    out["n_columns"] = len(cols)

    known = set(known_columns or [])
    roles = set(role_columns or []) | set(INPUT_SCHEMA.get("roles") or [])
    for c in cols:
        if c in known or c in roles:
            if c in roles:
                out["roles"].append(c)
            continue
        out["unknown"].append(c)
    # 未知列不判 OK/失败：合同的作用是把它们摆到明面上，
    # 由调用方决定「隔离」还是「放行」，而不是打个分就算过。
    if fields:
        cv = check_values(table, fields)
        out["values"] = {"checked": cv.get("checked"), "violations": cv.get("violations")}
        if not cv.get("ok"):
            out["ok"] = False
            out["violations"].extend(cv.get("violations") or [])
    return out


# ---------------------------------------------------------------- 分析结果契约
#
# 只登记**跨模块的公共结构**。某一环节的内部字段由该模块自己负责，
# 这里管的是"别人要消费你的输出时必须约定好的那部分"。
RESULT_SCHEMA = {
    "version": SCHEMA_VERSION,
    "required": [
        field("analyzer_version", type_="text", required=True, nullable=False,
              source="lib/pipeline.py::ANALYZER_VERSION",
              desc="产出这份结果的代码版本"),
        field("process", type_="text", required=False,
              desc="工序键；未识别出工序时允许为空但须披露"),
        field("target", type_="any", required=False, desc="被分析的目标量"),
        field("shape", type_="object", required=False, desc="(行数, 列数)"),
    ],
    # 每个环节如果要声称自己"算过了"，就得带 status；缺 status 视为未产出结论。
    "sections": [
        field("spc", type_="list", desc="SPC 判异结果"),
        field("defects", type_="list", desc="识别出的缺陷清单"),
        field("rootcause", type_="object", desc="根因候选排序"),
        field("association", type_="list", desc="目标关联度"),
    ],
}


def check_result(result, require_status=True):
    """校验分析结果对象。

    require_status=True 时，凡是存在却不带 status 的环节，
    都会被记一条 violation——这正是 P0-3 的约束：
    「没有 status 就不知道它到底算过没有，还是算崩了返回了 None」。
    """
    out = {
        "ok": True, "version": SCHEMA_VERSION,
        "missing_required": [], "missing_status": [],
        "violations": [], "n_sections": 0,
    }
    if not isinstance(result, dict):
        out["ok"] = False
        out["violations"].append("分析结果不是 dict，而是 %s" % type(result).__name__)
        return out

    ok, level, note = compatible(result.get("schema_version") or SCHEMA_VERSION)
    out["version_note"] = note
    if not ok:
        out["ok"] = False
        out["violations"].append(note)

    # 必填 terraces
    for f in RESULT_SCHEMA["required"]:
        n = f["name"]
        if f["required"] and (n not in result or result[n] in (None, "")):
            out["missing_required"].append(n)
    if out["missing_required"]:
        out["ok"] = False
        out["violations"].append(
            "结果缺少必填字段：%s" % "、".join(out["missing_required"]))

    # 环节必须有 status
    for f in RESULT_SCHEMA["sections"]:
        n = f["name"]
        v = result.get(n)
        if v is None:
            continue                                  # 没产出不等于不合规
        out["n_sections"] += 1
        if not require_status:
            continue
        # list 型环节（spc/defects…）的 status 挂在**每个元素**上，
        # 只看容器的 .get 会把整个 list 误判成"没 status"，那是假报。
        if isinstance(v, dict):
            samples = [v]
        elif isinstance(v, (list, tuple)):
            samples = [x for x in v if isinstance(x, dict)]
            if not samples:
                continue                      # 空列表=没产出，不算违规
        else:
            continue                          # 未知形态不硬判，交给调用方
        if not all(isinstance(s.get("status"), str) and s.get("status")
                   for s in samples):
            out["missing_status"].append(n)
    if out["missing_status"]:
        out["ok"] = False
        out["violations"].append(
            "以下环节有输出却没有 status，无法区分『算过且无发现』与『算崩了』：%s"
            % "、".join(out["missing_status"]))
    return out


def load_custom_schema(path):
    """加载调用方自己的字段契约（企业级，随项目走）。

    支持的形态：{"version": "1.x", "fields": [ {...}, ... ]}。
    加载失败要把原因说清楚——「文件路径对不对」和「文件里格式不对」
    对使用者是完全不同的两个问题，混成一句「加载失败」等于没说。
    """
    if not path:
        return {"ok": False, "error": "未提供契约文件路径"}
    if not os.path.exists(path):
        return {"ok": False, "error": "契约文件不存在：%s" % path}
    try:
        raw = io.open(path, encoding="utf-8").read()
    except Exception as exc:
        return {"ok": False, "error": "契约文件读不了：%s" % exc}
    try:
        d = json.loads(raw)
    except Exception as exc:
        return {"ok": False, "error": "契约文件不是合法 JSON：%s" % exc}
    if not isinstance(d, dict):
        return {"ok": False, "error": "契约文件顶层必须是对象"}
    ok, level, note = compatible(d.get("version"))
    return {"ok": ok, "level": level, "note": note, "schema": d}


def quarantine(unknown_columns, reason=None):
    """把未知列打包成隔离记录，挂到结果上供报告披露。

    隔离不是丢弃：数据照样留着，只是**不进结论**。
    这样使用者既能看到「有这几列没被采纳」，也不会因为少几列就以为报告是完整的。
    """
    return {
        "columns": list(unknown_columns or []),
        "n": len(unknown_columns or []),
        "reason": reason or "未在字段契约中登记，不参与本次工艺判定与结论",
    }

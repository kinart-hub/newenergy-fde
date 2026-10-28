# -*- coding: utf-8 -*-
"""口径声明与检查：单位 / 时区 / 班次（评审 P0-2）。

这三样东西有一个共同点：**不声明，工具就会静默替使用者假设一个口径**，
而那个假设一旦错了，相关性和 SPC 的结论就整体失真，且看起来完全正确。

  · 单位   —— 功率 p/kW、电量 Wh/Ah、厚度 mm/μm：不声明等于默许「列名里那个括号
             就是唯一真相」，但只要有一列漏了括号或写错，1000 倍的偏差就进了回归。
  · 时区   —— 时间戳不带时区时，工具只能按「本机时钟」解释。跨时区的 MES 导出
             （总部 UTC、产线 +0800）混在一起，同一时刻的两行会被当成隔了 8 小时。
  · 班次   —— 班次作为分组/混杂维度时，「班」的边界若没跟时间戳对齐（跨午夜、无
             班次定义），按班次分组就会出现「同一班的数据被拆成两半」。

本模块的原则与无效段模块一致：**能客观判断的自动判，判不了的一律显式声明「未确认」，
绝不静默假设**。未确认本身是一个结果，要写进报告，必要时让下游降级措辞。
"""

from __future__ import unicode_literals

_TZ_NAMES = ("Z", "UTC", "GMT", "CST", "EST", "PST", "MSK", "JST", "KST", "CET",
             "+08:00", "+0800", "Asia/", "America/", "Europe/", "Etc/")


# ---------------------------------------------------------------- 单位口径

def unit_scope(adapt_issues=None, conversions=None):
    """汇总本次分析的单位口径。

    输入可复用 adapt.py 已有的判定结论（unit_conflict / 换算 applied），不计重复造；
    这里只负责把它整理成一个"报告可打印、机器可判"的声明对象。

    返回 {
      status:   ok          —— 无单位冲突、无换算（列名单位的原始口径即可）
               converted   —— 有单位换算，换算来源与系数已登记
               conflict    —— 存在无法换算的单位冲突
               none        —— 没有任何单位信息可核对（列名都没有单位括号）
      used_units: {列: 单位}  本次实际采用的单位口径
      issues: [{column, from_unit, to_unit, factor, source, note}]
      notes:  []                人话说明
    }
    """
    used = {}
    issues = []
    notes = []

    for it in (conversions or []):
        if isinstance(it, dict):
            used[it.get("column") or it.get("to_col") or ""] = it.get("to")
            issues.append({
                "column": it.get("column") or it.get("to_col"),
                "from_unit": it.get("from"), "to_unit": it.get("to"),
                "factor": it.get("factor"), "source": it.get("source", "注册换算表"),
                "note": "已从 %s 换算为 %s（×%s）" % (
                    it.get("from"), it.get("to"), it.get("factor")),
            })
    for it in (adapt_issues or []):
        if not isinstance(it, dict):
            continue
        if it.get("type") == "unit_conflict":
            issues.append({
                "column": it.get("column"),
                "from_unit": it.get("unit_in_file"), "to_unit": it.get("unit_expected"),
                "factor": None, "source": "知识库",
                "note": "单位 %s 与库内登记 %s 不一致且无换算规则——该列退出工艺判定"
                        % (it.get("unit_in_file"), it.get("unit_expected")),
            })
            used.setdefault(it.get("column") or "", it.get("unit_in_file"))

    conflicts = [i for i in issues if i.get("factor") is None]
    convs = [i for i in issues if i.get("factor") is not None]
    if conflicts:
        status = "conflict"
    elif convs:
        status = "converted"
    elif issues:
        status = "ok"
    else:
        status = "none"
        notes.append("本次数据里没有显式单位信息（列名无单位括号）。"
                     "工具按「与知识库同参同单位」的假设处理——若导出设备的单位与"
                     "知识库登记不一致，相关性与 Cpk 会整体失真，请核对。")

    if convs:
        notes.append("本次有 %d 列发生了单位换算（未换算前与库内口径不一致），"
                     "换算口径如下表；换算系数取自注册换算表，不改原始数值。"
                     % len(convs))
    if conflicts:
        notes.append("有 %d 列单位无法换算，已退出工艺判定（数据保留）。"
                     "这属于**口径未对齐**，不是「那一列没有用」——请回源核对单位后重跑。"
                     % len(conflicts))
    return {"status": status, "used_units": used, "issues": issues, "notes": notes}


# ---------------------------------------------------------------- 时区口径

def timezone_scope(times=None, declared_tz=None, col_name=None):
    """时钟口径声明与跨时区检测。

    times          —— 解析出的 datetime 列表（表 5 的 time 列）
    declared_tz    —— 使用者显式登记的时区（如 "+08:00" / "Asia/Shanghai"）
    col_name       —— 时间列名，仅用于提示

    返回 {status, declared, naive, tzoff_offsets, notes}
      status: declared_ok / declared   —— 显式登记了时区
              offset                  —— 没登记，但从时间戳本身的偏移里读出了时区
              naive                   —— 时间戳不带任何时区信息，且未登记
              empty                   —— 没有时间列
    关键的"未确认"立场：naive 不是错误，是**必须显式声明的一声**——它意味着
    "跨时区数据混在一起没法查"，下游对'时序滞后/班次对齐'这类依赖绝对时刻的结论
    要自动降级措辞。
    """
    if not times:
        return {"status": "empty", "declared": declared_tz, "naive": True,
                "notes": ["没有时间列，本份数据不涉及时序口径。"], "col": col_name}

    if declared_tz:
        return {"status": "declared", "declared": declared_tz, "naive": False,
                "col": col_name, "tz_offsets": [],
                "notes": ["时区已显式登记为 %s；所有时间戳按此时区解释。" % declared_tz]}

    has_tz = []
    no_tz = 0
    for t in times:
        if t is None:
            continue
        if getattr(t, "tzinfo", None) is not None:
            has_tz.append(t)
        else:
            no_tz += 1
    # 带时区与不带时区的行**混在同一份数据里**本身就是冲突：不能只看带 tz 行的
    # 偏移一不一致，那会漏掉"一半行是 UTC、一半是不带时区的本地时间"这种情况。
    offsets = set()
    for t in has_tz:
        try:
            off = t.utcoffset()
            if off is not None:
                offsets.add(int(off.total_seconds()))
        except Exception:
            # scan:keep 个别时间点的 utcoffset() 取不到是正常情形（异常时区对象），
            # 这一行不影响"带不带时区"的判定，跳过即可，记账只会污染逐点热路径。
            pass
    if has_tz and no_tz:
        return {"status": "conflict", "declared": None, "naive": False,
                "col": col_name, "tz_offsets": sorted(offsets),
                "notes": ["同一份数据里既有带时区的时间戳（%d 行）、又有不带时区的"
                          "（%d 行）——后者会被按本机时钟解释，与前者差出整段时差。"
                          "请用 --timezone 显式登记统一时区。"
                          % (len(has_tz), no_tz)]}

    if has_tz and len(offsets) == 1:
        off = sorted(offsets)[0]
        hh, mm = divmod(abs(off) // 60, 60)
        sign = "+" if off >= 0 else "-"
        return {"status": "offset", "declared": "%s%02d:%02d" % (sign, hh, mm),
                "naive": False, "col": col_name, "tz_offsets": sorted(offsets),
                "notes": ["时间戳自带时区偏移 %s（%d 个时间点均一致）。"
                          % ("%s%02d:%02d" % (sign, hh, mm), len(has_tz))]}
    if has_tz and len(offsets) > 1:
        return {"status": "conflict", "declared": None, "naive": False,
                "col": col_name, "tz_offsets": sorted(offsets),
                "notes": ["时间戳里混着 %d 种不同的时区偏移（%s）——同一份数据里"
                          "部分行是 UTC、部分是本地时间。若不归一，同一时刻会被当成"
                          "隔了几个小时。请用 --timezone 显式登记统一时区。"
                          % (len(offsets),
                             "、".join("%+05d" % (o // 60 if False else o) for o in sorted(offsets)))]}

    # 什么都没带：这是最常见的真实情况（产线 MES 导出的 CSV 时间戳几乎都不带时区）。
    # 必须显式声明"按无时区处理"，并把时序依赖类结论的置信度吃掉一格。
    return {"status": "naive", "declared": None, "naive": True, "col": col_name,
            "tz_offsets": [],
            "notes": ["时间戳不含时区信息，本次按「无时区」处理——即假定所有行都在"
                      "同一时区。若数据跨时区（总部 UTC + 产线本地时间），"
                      "「时序滞后」「班次边界」这类依赖绝对时刻的结论可能失真；"
                      "请用 --timezone 登记。（列：%s）" % (col_name or "")]}


# ---------------------------------------------------------------- 班次口径

def shift_scope(table=None, shift_col=None, times=None, declared_boundary=None):
    """班次作为一个分组/混杂维度的口径。

    班次在分析里有两种完全不同的用法，口径不同，混用会出错：
      1. 作为混杂/分组维度（「哪一班干的」）——这不要求时间对齐，找的是差异；
      2. 作为时间语义（「跨午夜该算上一班还是下一班」）——这要求边界定义。

    本函数只回答「边界是否可核对」：
      - 显式登记了班次边界 / 从数据里读出了一致边界 → ok
      - 没有边界定义，且检查到跨午夜的样本 → warn（班次分组可能被午夜切断）
      - 没有班次列 → none（不涉及）
    """
    if not shift_col:
        return {"status": "none", "notes": ["数据里没有班次列，不涉及班次口径。"]}

    if declared_boundary:
        return {"status": "ok", "boundary": declared_boundary,
                "notes": ["班次边界已显式登记为 %s。" % declared_boundary]}

    # 没有边界定义：检查班次列取值是否是"时间制"的（早/中/晚、白/夜、A/B/C）。
    # 这里不做硬判断——班次命名体系太多，"哪个班跨午夜"超出了数据能说的范围。
    vals = []
    if table is not None and shift_col in getattr(table, "columns", []):
        try:
            vals = [str(v) for v in table.column(shift_col) if v is not None]
        except Exception:
            vals = []
    uniq = sorted(set(vals))
    if not vals:
        # 有班次列但拿不到取值（没传表）。这时更该保守：边界没法核对，出声。
        return {"status": "warn", "boundary": None,
                "notes": ["检测到班次列「%s」，但其取值未被取到、且班次边界未登记。"
                          "若产线跨午夜换班，同日不同班的数据会被混淆。"
                          "请用 --shift-boundary 登记各班起止时刻，或确认班次仅作"
                          "分组维度、不做时间语义。" % shift_col]}
    shift_like = len(uniq) <= 8 and all(
        len(u) <= 2 for u in uniq)  # "早班/晚班/白班/甲/A/B/C" 都是 ≤2 字标签；
                                     # "张三组/李四组"（3 字）是人员/班组，不是轮班时间制
    if not shift_like:
        return {"status": "ok", "notes": [
            "班次列「%s」取值 %s——更像人员/班组名称而非轮班时间制，"
            "按普通分组维度处理（不做跨午夜边界检查）。"
            % (shift_col, "、".join(uniq[:5]))]}

    return {"status": "warn", "boundary": None,
            "notes": ["班次列「%s」有 %d 个取值（%s），但**班次边界未登记**。"
                      "若产线跨午夜换班，同一班的数据会被时间戳切到两天、"
                      "分组统计被拆成两半。请用 --shift-boundary 登记各班的起止"
                      "时刻（如 早:06:00-14:00 / 中:14:00-22:00 / 夜:22:00-06:00）。"
                      % (shift_col, len(uniq), "、".join(uniq[:6]))]}


# ---------------------------------------------------------------- 汇总入口

def scope(adapt_issues=None, conversions=None, times=None, declared_tz=None,
          time_col=None, shift_col=None, declared_boundary=None, table=None):
    """一次性出具三份口径声明。返回 {unit, timezone, shift, downgrades}。"""
    unit = unit_scope(adapt_issues, conversions)
    tz = timezone_scope(times, declared_tz, time_col)
    shift = shift_scope(table, shift_col, times, declared_boundary)
    downgrades = []
    if unit["status"] == "conflict":
        downgrades.append("unit")
    if tz["status"] in ("naive", "conflict"):
        downgrades.append("timezone")
    if shift["status"] == "warn":
        downgrades.append("shift")
    return {"unit": unit, "timezone": tz, "shift": shift,
            "downgrades": downgrades}

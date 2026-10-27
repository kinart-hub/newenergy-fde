# -*- coding: utf-8 -*-
"""降级台账 —— 让每一个被吞掉的异常都留下一笔。

## 为什么要有这个模块

2026-10-26 全库静态扫描出 54 处 `except Exception: pass`。
逐处改成打印日志仍是打补丁，真正的问题是它们共有的那个模式：

    try:
        result = 算点什么()
    except Exception:
        pass
    return result          # 崩了这里就是 None

下游拿到 `None` 时，**分不清「确实没有」和「算崩了」**——
两种情况下报告长得一模一样，都是"这一节没有结论"。
于是最危险的一类缺陷诞生了：不崩、不报、不留痕，问题只在结论里表现。

## 本模块做什么

不改任何函数签名，只要求 except 分支调用一次 `note()`：

    try:
        pvals.append(t_sf(t, df))
    except Exception as e:
        degrade.note("stats.ols.tvals", e)

分析结束时 `drain()` 取走全部记录，汇入 result 与报告。
使用者看到的是「这次有 N 个环节是降级跑出来的，分别是……」，
而不是一份每个章节都写着"未检出"的完整报告。

## 设计取舍

- **不抛异常向上传递**。这些 except 存在的理由就是"单点失败降级而非中断"
  （项目既有护栏原则），把它们改成抛异常会让一张奇怪的表带走整轮分析。
  台账记的是"降级了"，不是"失败了"。
- **不自动改写结论**。台账只负责让降级可见，由使用者/报告决定怎么呈现。
- **进程内全局 + drain 取走**。分析是一次性批处理，不需要跨进程；
  drain 语义保证两次分析之间不串台。
- **note() 自身永不抛**。台账是诊断通道，诊断通道把主流程搞崩是不可接受的。
"""

import sys
import threading

_LOCK = threading.Lock()
_LOG = []          # [{"where","kind","msg","stage","n","first_msg"}]
_IDX = {}          # where -> _LOG 下标，用于合并同类
_MAX = 500         # 上限，防止病态数据下刷爆内存


def note(where, exc=None, stage=""):
    """记一笔降级。

    where —— 定位串，建议写「模块.函数.做什么」，如 "stats.ols.tvals"
    exc   —— 异常对象；也可以直接传字符串
    stage —— 所属阶段（read/clean/spc/rootcause/report…），可空
    """
    try:
        if isinstance(exc, str):
            kind, msg = "note", exc
        elif exc is not None:
            kind = type(exc).__name__
            msg = str(exc)
        else:
            kind, msg = "unknown", ""
        # P2-4：降级台账是所有护栏 except 的唯一收口点，在这里转发一次，
        # observ 就白拿到"哪个模块失败过、失败属于哪一类"，不必改 50 个调用点。
        # 必须放在去重判断**之前**：同类降级会被台账合并（信息量等于一次），
        # 但模块成功率要数真实发生次数——放在合并分支之后，重复失败就漏记了。
        # 台账自身仍必须静默（诊断通道不许搞崩主流程），所以这层也包在 try 里。
        try:
            from . import observ as _OBS        # 惰性导入：observ 不反向依赖 degrade
            _OBS.fail(str(where).split(".")[0] or "unknown",
                      exc if exc is not None else (msg or "降级"),
                      stage=stage, where=str(where))
        except Exception:
            pass
            # scan:keep 转发到 observ 失败必须静默：这里抛异常会被 note() 最外层再捕获一次，
            # 而"记账失败"不该让主流程降级失败。可观测性缺失由 observ.self_errors 单独暴露。
        with _LOCK:
            w = str(where)
            if w in _IDX:
                # 同类合并：同一个位置失败一千次，信息量等于一次。
                # 不合并的话，一次分析能刷出上百条重复记录（实测 to_number 就是这样），
                # 使用者会直接忽略整个台账。
                _LOG[_IDX[w]]["n"] += 1
                return
            if len(_LOG) >= _MAX:
                return
            _IDX[w] = len(_LOG)
            _LOG.append({"where": w, "kind": kind, "msg": msg[:300],
                         "stage": stage, "n": 1})
    except Exception:
        # 台账自身出任何问题都必须静默——诊断通道不许反过来搞崩主流程
        pass
        # scan:keep 台账自身必须静默：记账再抛异常会递归回这里，诊断通道不许反过来搞崩主流程。退化路径是 print，不是崩溃。


def note_current(where, stage=""):
    """在 except 分支里无参数地记一笔：自动取当前异常。"""
    try:
        exc = sys.exc_info()[1]
    except Exception:
        exc = None
    note(where, exc, stage)


def drain():
    """取走并清空全部记录（保证两次分析之间不串台）。"""
    with _LOCK:
        out = list(_LOG)
        _LOG[:] = []
        _IDX.clear()
    return out


def peek():
    """只看不取。"""
    with _LOCK:
        return list(_LOG)


def count():
    with _LOCK:
        return len(_LOG)


def summary_lines(limit=20):
    """给报告用的人类可读摘要。空台账返回空列表（报告里就不出现这一节）。"""
    items = peek()
    if not items:
        return []
    n_total = sum(it.get("n", 1) for it in items)
    out = ["本次分析有 %d 类环节降级运行（共 %d 次；不是失败，但该环节的证据缺失）："
           % (len(items), n_total)]
    for it in items[:limit]:
        _n = it.get("n", 1)
        _rep = "（重复 %d 次）" % _n if _n > 1 else ""
        out.append("  · %s：%s %s%s" % (it["where"], it["kind"], it["msg"], _rep))
    if len(items) > limit:
        out.append("  · ……另有 %d 处，见 degradation_log 字段" % (len(items) - limit))
    return out

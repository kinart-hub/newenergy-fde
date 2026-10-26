# -*- coding: utf-8 -*-
"""
charts.py —— 零依赖 SVG 图表

不依赖 matplotlib。输出 SVG 字符串，可：
  - 内嵌进 HTML 报告（单文件自包含，离线可看）
  - 单独存为 .svg 供 PPT / Word / 邮件直接使用

图表：SPC 控制图（含 A/B/C 分区与判异点高亮）、直方图+规格线+正态曲线、
相关性热力图、Pareto 图、分组箱线图、时间序列趋势图、散点+回归线、主效应图。
"""

import math
from . import stats as S

FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI','Microsoft YaHei',sans-serif"

C_BLUE = "#2563eb"
C_RED = "#dc2626"
C_GREEN = "#16a34a"
C_GRAY = "#64748b"
C_LIGHT = "#e2e8f0"
C_ORANGE = "#ea580c"


def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _nice_ticks(lo, hi, n=5):
    if lo == hi:
        lo, hi = lo - 1, hi + 1
    span = hi - lo
    raw = span / n
    mag = 10 ** math.floor(math.log10(abs(raw))) if raw else 1
    for m in (1, 2, 2.5, 5, 10):
        if raw / mag <= m:
            step = m * mag
            break
    else:
        step = 10 * mag
    start = math.floor(lo / step) * step
    out = []
    v = start
    while v <= hi + step * 0.5:
        out.append(v)
        v += step
    return out, step


def _fmt(v, step):
    if v is None:
        return ""
    if step and step < 0.01:
        return "%.4f" % v
    if step and step < 1:
        return "%.2f" % v
    if abs(v) >= 10000:
        return "%.0f" % v
    if abs(v) >= 100:
        return "%.1f" % v
    return "%.2f" % v


# ------------------------------------------------------------------ 控制图

def control_chart(values, center, lcl, ucl, title="控制图", ylabel="",
                  violations=None, labels=None, width=760, height=340,
                  spec_lines=None):
    vals = [v for v in values if v is not None]
    if not vals:
        return ""
    W, H = width, height
    ml, mr, mt, mb = 62, 22, 40, 52
    pw, ph = W - ml - mr, H - mt - mb
    lo = min(vals + [x for x in (lcl, ucl) if x is not None])
    hi = max(vals + [x for x in (lcl, ucl) if x is not None])
    if spec_lines:
        valid_spec = [s for s in spec_lines if s is not None]
        if valid_spec:
            lo = min(lo, min(valid_spec))
            hi = max(hi, max(valid_spec))
    pad = (hi - lo) * 0.12 or 1
    lo, hi = lo - pad, hi + pad
    ticks, step = _nice_ticks(lo, hi, 5)

    def Y(v):
        return mt + ph - (v - lo) / (hi - lo) * ph

    def X(i):
        n = max(1, len(values) - 1)
        return ml + i / n * pw

    s = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" width="%d" height="%d" font-family="%s">'
         % (W, H, W, H, FONT)]
    s.append('<rect width="%d" height="%d" fill="#ffffff"/>' % (W, H))
    s.append('<text x="%d" y="24" font-size="14" font-weight="600" fill="#0f172a">%s</text>'
             % (ml, _esc(title)))
    # A/B/C 分区
    if center is not None and ucl is not None:
        half = (ucl - center)
        if half and half > 0:
            for frac, col in ((1.0, "#fef08a"), (2.0 / 3, "#fef9c3")):
                s.append('<rect x="%d" y="%.1f" width="%d" height="%.1f" fill="%s" opacity="0.55"/>'
                         % (ml, Y(center + half * frac), pw,
                            abs(Y(center - half * frac) - Y(center + half * frac)), col))
            s.append('<rect x="%d" y="%.1f" width="%d" height="%.1f" fill="#dcfce7" opacity="0.5"/>'
                     % (ml, Y(center + half / 3), pw, abs(Y(center - half / 3) - Y(center + half / 3))))
    # 网格与刻度
    for t in ticks:
        y = Y(t)
        s.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="%s" stroke-width="1"/>'
                 % (ml, y, ml + pw, y, C_LIGHT))
        s.append('<text x="%d" y="%.1f" font-size="10" fill="%s" text-anchor="end">%s</text>'
                 % (ml - 6, y + 3, C_GRAY, _fmt(t, step)))
    # 控制限与中心线
    if center is not None:
        s.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="%s" stroke-width="1.6"/>'
                 % (ml, Y(center), ml + pw, Y(center), C_GREEN))
        s.append('<text x="%d" y="%.1f" font-size="10" fill="%s">CL %.4g</text>'
                 % (ml + pw - 78, Y(center) - 4, C_GREEN, center))
    for lim, col, nm in ((ucl, C_RED, "UCL"), (lcl, C_RED, "LCL")):
        if lim is not None:
            s.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="%s" stroke-width="1.4" stroke-dasharray="6 4"/>'
                     % (ml, Y(lim), ml + pw, Y(lim), col))
            s.append('<text x="%d" y="%.1f" font-size="10" fill="%s">%s %.4g</text>'
                     % (ml + pw - 78, Y(lim) - 4, col, nm, lim))
    # 规格线
    for sv in (spec_lines or []):
        if sv is None:
            continue
        s.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="%s" stroke-width="1.4" stroke-dasharray="3 3"/>'
                 % (ml, Y(sv), ml + pw, Y(sv), C_ORANGE))
    # 数据折线
    pts = [(X(i), Y(v)) for i, v in enumerate(values) if v is not None]
    if len(pts) > 1:
        s.append('<polyline points="%s" fill="none" stroke="%s" stroke-width="1.5"/>'
                 % (" ".join("%.1f,%.1f" % p for p in pts), C_BLUE))
    # 点（判异点红色）
    bad = set()
    for vio in (violations or []):
        for i in vio.get("indexes", []):
            bad.add(i)
    for i, v in enumerate(values):
        if v is None:
            continue
        col = C_RED if i in bad else C_BLUE
        s.append('<circle cx="%.1f" cy="%.1f" r="%s" fill="%s"/>'
                 % (X(i), Y(v), "3.6" if i in bad else "2.6", col))
    # 轴标签
    s.append('<text x="%d" y="%d" font-size="10" fill="%s" text-anchor="middle">%s</text>'
             % (ml + pw // 2, H - 12, C_GRAY, _esc(ylabel or "样本序号")))
    s.append('</svg>')
    return "".join(s)


# ------------------------------------------------------------------ 直方图

def histogram(values, bins=20, lsl=None, usl=None, target=None,
              title="分布直方图", xlabel="", width=760, height=340):
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return ""
    W, H = width, height
    ml, mr, mt, mb = 62, 22, 40, 52
    pw, ph = W - ml - mr, H - mt - mb
    lo, hi = min(vals), max(vals)
    if lsl is not None:
        lo = min(lo, lsl)
    if usl is not None:
        hi = max(hi, usl)
    pad = (hi - lo) * 0.08 or 1
    lo, hi = lo - pad, hi + pad
    w = (hi - lo) / bins
    counts = [0] * bins
    for v in vals:
        i = min(bins - 1, int((v - lo) / w))
        counts[i] += 1
    cmax = max(counts) or 1
    mu, sd = S.mean(vals), S.stdev(vals)
    # 正态密度（按面积缩放）
    def dens(x):
        if not sd:
            return 0
        return math.exp(-((x - mu) ** 2) / (2 * sd * sd)) / (sd * math.sqrt(2 * math.pi))

    scale = len(vals) * w
    s = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" width="%d" height="%d" font-family="%s">'
         % (W, H, W, H, FONT)]
    s.append('<rect width="%d" height="%d" fill="#ffffff"/>' % (W, H))
    s.append('<text x="%d" y="24" font-size="14" font-weight="600" fill="#0f172a">%s</text>' % (ml, _esc(title)))

    def Y(c):
        return mt + ph - c / cmax * ph

    def X(v):
        return ml + (v - lo) / (hi - lo) * pw

    ticks, step = _nice_ticks(lo, hi, 6)
    for t in ticks:
        s.append('<line x1="%.1f" y1="%d" x2="%.1f" y2="%d" stroke="%s" stroke-width="1"/>'
                 % (X(t), mt, X(t), mt + ph, C_LIGHT))
        s.append('<text x="%.1f" y="%d" font-size="10" fill="%s" text-anchor="middle">%s</text>'
                 % (X(t), mt + ph + 16, C_GRAY, _fmt(t, step)))
    for i, c in enumerate(counts):
        x0, x1 = X(lo + i * w), X(lo + (i + 1) * w)
        s.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="%s" opacity="0.85"/>'
                 % (x0, Y(c), max(1, x1 - x0 - 1), mt + ph - Y(c), C_BLUE))
    # 规格线
    for v, col, nm in ((lsl, C_ORANGE, "LSL"), (usl, C_ORANGE, "USL"), (target, C_GREEN, "Target")):
        if v is not None and lo <= v <= hi:
            s.append('<line x1="%.1f" y1="%d" x2="%.1f" y2="%d" stroke="%s" stroke-width="1.6" stroke-dasharray="5 3"/>'
                     % (X(v), mt, X(v), mt + ph, col))
            s.append('<text x="%.1f" y="%d" font-size="10" fill="%s" text-anchor="middle">%s</text>'
                     % (X(v), mt - 6, col, nm))
    # 正态曲线
    if sd:
        pts = []
        for i in range(101):
            x = lo + (hi - lo) * i / 100
            pts.append("%.1f,%.1f" % (X(x), Y(dens(x) * scale)))
        s.append('<polyline points="%s" fill="none" stroke="%s" stroke-width="1.8"/>' % (" ".join(pts), C_RED))
    s.append('<text x="%d" y="%d" font-size="10" fill="%s" text-anchor="middle">%s</text>'
             % (ml + pw // 2, H - 12, C_GRAY, _esc(xlabel)))
    # 统计摘要
    s.append('<text x="%d" y="%d" font-size="10" fill="%s">n=%d  μ=%.4g  σ=%.4g</text>'
             % (ml + pw - 200, mt + 12, C_GRAY, len(vals), mu, sd or 0))
    s.append('</svg>')
    return "".join(s)


# ------------------------------------------------------------------ 热力图

def heatmap(matrix, labels, title="相关性矩阵", width=760, height=None):
    n = len(labels)
    if n == 0:
        return ""
    cell = 44
    ml, mt = 110, 60
    pw = ph_ = cell * n
    H = (height or (mt + ph_ + 60))
    W = width
    s = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" width="%d" height="%d" font-family="%s">'
         % (W, H, W, H, FONT)]
    s.append('<rect width="%d" height="%d" fill="#ffffff"/>' % (W, H))
    s.append('<text x="20" y="24" font-size="14" font-weight="600" fill="#0f172a">%s</text>' % _esc(title))
    for i in range(n):
        for j in range(n):
            v = matrix[i][j]
            if v is None:
                col = "#f1f5f9"
            else:
                v = max(-1.0, min(1.0, v))
                if v >= 0:
                    t = v
                    r = int(255 - (255 - 220) * t)
                    g = int(255 - (255 - 38) * t)
                    b = int(255 - (255 - 38) * t)
                else:
                    t = -v
                    r = int(255 - (255 - 37) * t)
                    g = int(255 - (255 - 99) * t)
                    b = int(255 - (255 - 235) * t)
                col = "rgb(%d,%d,%d)" % (r, g, b)
            x = ml + j * cell
            y = mt + i * cell
            s.append('<rect x="%d" y="%d" width="%d" height="%d" fill="%s" stroke="#ffffff"/>'
                     % (x, y, cell, cell, col))
            if v is not None and n <= 14:
                s.append('<text x="%d" y="%d" font-size="10" fill="%s" text-anchor="middle">%.2f</text>'
                         % (x + cell // 2, y + cell // 2 + 4,
                            "#0f172a" if abs(v) < 0.6 else "#ffffff", v))
    for i, lb in enumerate(labels):
        txt = lb if len(lb) <= 12 else lb[:11] + "…"
        s.append('<text x="%d" y="%d" font-size="10" fill="%s" text-anchor="end">%s</text>'
                 % (ml - 6, mt + i * cell + cell // 2 + 4, C_GRAY, _esc(txt)))
        s.append('<text transform="translate(%d,%d) rotate(-40)" font-size="10" fill="%s">%s</text>'
                 % (ml + i * cell + 4, mt - 6, C_GRAY, _esc(txt)))
    # 图例
    lx, ly = 20, H - 30
    for k in range(21):
        t = k / 20 * 2 - 1
        if t >= 0:
            col = "rgb(%d,%d,%d)" % (int(255 - 35 * t), int(255 - 217 * t), int(255 - 217 * t))
        else:
            t2 = -t
            col = "rgb(%d,%d,%d)" % (int(255 - 218 * t2), int(255 - 156 * t2), int(255 - 20 * t2))
        s.append('<rect x="%d" y="%d" width="10" height="12" fill="%s"/>' % (lx + k * 10, ly, col))
    s.append('<text x="%d" y="%d" font-size="10" fill="%s">-1</text>' % (lx - 4, ly + 22, C_GRAY))
    s.append('<text x="%d" y="%d" font-size="10" fill="%s">+1</text>' % (lx + 200, ly + 22, C_GRAY))
    s.append('</svg>')
    return "".join(s)


# ------------------------------------------------------------------ Pareto

def pareto_chart(rows, title="不良 Pareto", width=760, height=360):
    if not rows:
        return ""
    W, H = width, height
    ml, mr, mt, mb = 62, 56, 40, 74
    pw, ph = W - ml - mr, H - mt - mb
    cmax = max(r["value"] for r in rows) or 1
    n = len(rows)
    bw = pw / n * 0.62
    gap = pw / n

    def Y(v):
        return mt + ph - v / cmax * ph

    def YP(p):
        return mt + ph - p / 100.0 * ph

    s = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" width="%d" height="%d" font-family="%s">'
         % (W, H, W, H, FONT)]
    s.append('<rect width="%d" height="%d" fill="#ffffff"/>' % (W, H))
    s.append('<text x="%d" y="24" font-size="14" font-weight="600" fill="#0f172a">%s</text>' % (ml, _esc(title)))
    for t in (0, 25, 50, 75, 100):
        s.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="%s" stroke-width="1"/>'
                 % (ml, YP(t), ml + pw, YP(t), C_LIGHT))
        s.append('<text x="%d" y="%.1f" font-size="10" fill="%s" text-anchor="end">%d%%</text>'
                 % (ml + pw + 26, YP(t) + 3, C_GRAY, t))
    ticks, step = _nice_ticks(0, cmax, 4)
    for t in ticks:
        s.append('<text x="%d" y="%.1f" font-size="10" fill="%s" text-anchor="end">%s</text>'
                 % (ml - 6, Y(t) + 3, C_GRAY, _fmt(t, step)))
    for i, r in enumerate(rows):
        x = ml + i * gap + (gap - bw) / 2
        col = {"A": C_RED, "B": C_ORANGE, "C": C_BLUE}.get(r.get("abc"), C_BLUE)
        s.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="%s" opacity="0.88"/>'
                 % (x, Y(r["value"]), bw, mt + ph - Y(r["value"]), col))
        s.append('<text x="%.1f" y="%.1f" font-size="10" fill="%s" text-anchor="middle">%s</text>'
                 % (x + bw / 2, Y(r["value"]) - 5, C_GRAY, _fmt(r["value"], step)))
        lb = r["item"] if len(str(r["item"])) <= 8 else str(r["item"])[:7] + "…"
        s.append('<text transform="translate(%.1f,%d) rotate(-25)" font-size="10" fill="%s">%s</text>'
                 % (x + bw / 2, mt + ph + 16, "#334155", _esc(lb)))
    pts = [(ml + i * gap + gap / 2, YP(r["cum_pct"])) for i, r in enumerate(rows)]
    s.append('<polyline points="%s" fill="none" stroke="%s" stroke-width="1.8"/>'
             % (" ".join("%.1f,%.1f" % p for p in pts), "#0f172a"))
    for p, r in zip(pts, rows):
        s.append('<circle cx="%.1f" cy="%.1f" r="3" fill="%s"/>' % (p[0], p[1], "#0f172a"))
        s.append('<text x="%.1f" y="%.1f" font-size="9" fill="%s" text-anchor="middle">%.1f</text>'
                 % (p[0], p[1] - 7, "#0f172a", r["cum_pct"]))
    s.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="%s" stroke-width="1.2" stroke-dasharray="5 4"/>'
             % (ml, YP(80), ml + pw, YP(80), C_GREEN))
    s.append('<text x="%d" y="%.1f" font-size="10" fill="%s">80%% 线（A 类）</text>' % (ml + 4, YP(80) - 5, C_GREEN))
    s.append('</svg>')
    return "".join(s)


# ------------------------------------------------------------------ 分组箱线

def boxplot(groups, title="分组分布对比", ylabel="", width=760, height=340):
    """groups: [{"group":..,"values":[..]}]"""
    groups = [g for g in groups if g.get("values")]
    if not groups:
        return ""
    W, H = width, height
    ml, mr, mt, mb = 62, 22, 40, 60
    pw, ph = W - ml - mr, H - mt - mb
    allv = [v for g in groups for v in g["values"]]
    lo, hi = min(allv), max(allv)
    pad = (hi - lo) * 0.12 or 1
    lo, hi = lo - pad, hi + pad
    n = len(groups)
    bw = pw / n * 0.5
    gap = pw / n

    def Y(v):
        return mt + ph - (v - lo) / (hi - lo) * ph

    s = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" width="%d" height="%d" font-family="%s">'
         % (W, H, W, H, FONT)]
    s.append('<rect width="%d" height="%d" fill="#ffffff"/>' % (W, H))
    s.append('<text x="%d" y="24" font-size="14" font-weight="600" fill="#0f172a">%s</text>' % (ml, _esc(title)))
    ticks, step = _nice_ticks(lo, hi, 5)
    for t in ticks:
        s.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="%s" stroke-width="1"/>'
                 % (ml, Y(t), ml + pw, Y(t), C_LIGHT))
        s.append('<text x="%d" y="%.1f" font-size="10" fill="%s" text-anchor="end">%s</text>'
                 % (ml - 6, Y(t) + 3, C_GRAY, _fmt(t, step)))
    for i, g in enumerate(groups):
        vs = sorted(g["values"])
        cx = ml + i * gap + gap / 2
        q1, q2, q3 = S.quantile(vs, .25), S.quantile(vs, .5), S.quantile(vs, .75)
        iqr = q3 - q1
        ul = min(max(vs), q3 + 1.5 * iqr)
        ll = max(min(vs), q1 - 1.5 * iqr)
        s.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" stroke-width="1.2"/>'
                 % (cx, Y(ul), cx, Y(ll), C_GRAY))
        for v, w_ in ((ul, 6), (ll, 6)):
            s.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" stroke-width="1.2"/>'
                     % (cx - w_, Y(v), cx + w_, Y(v), C_GRAY))
        s.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="%s" opacity="0.25" stroke="%s"/>'
                 % (cx - bw / 2, Y(q3), bw, abs(Y(q1) - Y(q3)), C_BLUE, C_BLUE))
        s.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" stroke-width="2"/>'
                 % (cx - bw / 2, Y(q2), cx + bw / 2, Y(q2), C_RED))
        s.append('<text x="%.1f" y="%d" font-size="10" fill="%s" text-anchor="middle">%s</text>'
                 % (cx, mt + ph + 16, "#334155", _esc(str(g["group"])[:10])))
        s.append('<text x="%.1f" y="%d" font-size="9" fill="%s" text-anchor="middle">n=%d</text>'
                 % (cx, mt + ph + 30, C_GRAY, len(vs)))
    s.append('<text x="%d" y="%d" font-size="10" fill="%s" text-anchor="middle">%s</text>'
             % (ml + pw // 2, H - 12, C_GRAY, _esc(ylabel)))
    s.append('</svg>')
    return "".join(s)


# ------------------------------------------------------------------ 时序

# scan:keep 库能力：时间序列图。当前报告只用到控制图/主效应之外的基础图，时间序列待时序分析章节启用时接上（届时报告第 5 节会调用）。
def time_series(x_labels, series, title="时间序列", ylabel="", width=760, height=320,
                hlines=None):
    """series: [{"name":..,"values":[..],"color":..}]"""
    W, H = width, height
    ml, mr, mt, mb = 62, 22, 40, 52
    pw, ph = W - ml - mr, H - mt - mb
    allv = [v for s_ in series for v in s_["values"] if v is not None]
    if not allv:
        return ""
    lo, hi = min(allv), max(allv)
    pad = (hi - lo) * 0.1 or 1
    lo, hi = lo - pad, hi + pad
    ticks, step = _nice_ticks(lo, hi, 5)

    def Y(v):
        return mt + ph - (v - lo) / (hi - lo) * ph

    def X(i):
        n = max(1, len(x_labels) - 1)
        return ml + i / n * pw

    s = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" width="%d" height="%d" font-family="%s">'
         % (W, H, W, H, FONT)]
    s.append('<rect width="%d" height="%d" fill="#ffffff"/>' % (W, H))
    s.append('<text x="%d" y="24" font-size="14" font-weight="600" fill="#0f172a">%s</text>' % (ml, _esc(title)))
    for t in ticks:
        s.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="%s" stroke-width="1"/>'
                 % (ml, Y(t), ml + pw, Y(t), C_LIGHT))
        s.append('<text x="%d" y="%.1f" font-size="10" fill="%s" text-anchor="end">%s</text>'
                 % (ml - 6, Y(t) + 3, C_GRAY, _fmt(t, step)))
    for hv in (hlines or []):
        if hv.get("value") is not None:
            s.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="%s" stroke-width="1.2" stroke-dasharray="5 4"/>'
                     % (ml, Y(hv["value"]), ml + pw, Y(hv["value"]), hv.get("color", C_ORANGE)))
    palette = [C_BLUE, C_RED, C_GREEN, C_ORANGE, "#7c3aed", "#0891b2"]
    for k, se in enumerate(series):
        col = se.get("color") or palette[k % len(palette)]
        pts = [(X(i), Y(v)) for i, v in enumerate(se["values"]) if v is not None]
        if len(pts) > 1:
            s.append('<polyline points="%s" fill="none" stroke="%s" stroke-width="1.5"/>'
                     % (" ".join("%.1f,%.1f" % p for p in pts), col))
        s.append('<text x="%d" y="%d" font-size="10" fill="%s">— %s</text>'
                 % (ml + 6 + k * 110, mt + 12, col, _esc(se["name"])))
    step_lb = max(1, len(x_labels) // 8)
    for i in range(0, len(x_labels), step_lb):
        s.append('<text x="%.1f" y="%d" font-size="9" fill="%s" text-anchor="middle">%s</text>'
                 % (X(i), mt + ph + 16, C_GRAY, _esc(str(x_labels[i])[:16])))
    s.append('<text x="%d" y="%d" font-size="10" fill="%s" text-anchor="middle">%s</text>'
             % (ml + pw // 2, H - 12, C_GRAY, _esc(ylabel)))
    s.append('</svg>')
    return "".join(s)


# ------------------------------------------------------------------ 散点+回归

def scatter(xs, ys, xlabel="", ylabel="", title="相关性散点图", width=640, height=380):
    pairs = [(a, b) for a, b in zip(xs, ys) if a is not None and b is not None]
    if len(pairs) < 3:
        return ""
    W, H = width, height
    ml, mr, mt, mb = 62, 22, 40, 52
    pw, ph = W - ml - mr, H - mt - mb
    xa = [p[0] for p in pairs]
    ya = [p[1] for p in pairs]
    xlo, xhi = min(xa), max(xa)
    ylo, yhi = min(ya), max(ya)
    xp = (xhi - xlo) * 0.08 or 1
    yp = (yhi - ylo) * 0.08 or 1
    xlo, xhi, ylo, yhi = xlo - xp, xhi + xp, ylo - yp, yhi + yp

    def X(v):
        return ml + (v - xlo) / (xhi - xlo) * pw

    def Y(v):
        return mt + ph - (v - ylo) / (yhi - ylo) * ph

    fit = S.linear_regression([[a] for a in xa], ya)
    r, p, n = S.pearson(xa, ya)
    s = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" width="%d" height="%d" font-family="%s">'
         % (W, H, W, H, FONT)]
    s.append('<rect width="%d" height="%d" fill="#ffffff"/>' % (W, H))
    s.append('<text x="%d" y="24" font-size="14" font-weight="600" fill="#0f172a">%s</text>' % (ml, _esc(title)))
    xt, xs_ = _nice_ticks(xlo, xhi, 5)
    yt, ys_ = _nice_ticks(ylo, yhi, 5)
    for t in xt:
        s.append('<line x1="%.1f" y1="%d" x2="%.1f" y2="%d" stroke="%s" stroke-width="1"/>'
                 % (X(t), mt, X(t), mt + ph, C_LIGHT))
        s.append('<text x="%.1f" y="%d" font-size="10" fill="%s" text-anchor="middle">%s</text>'
                 % (X(t), mt + ph + 16, C_GRAY, _fmt(t, xs_)))
    for t in yt:
        s.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="%s" stroke-width="1"/>'
                 % (ml, Y(t), ml + pw, Y(t), C_LIGHT))
        s.append('<text x="%d" y="%.1f" font-size="10" fill="%s" text-anchor="end">%s</text>'
                 % (ml - 6, Y(t) + 3, C_GRAY, _fmt(t, ys_)))
    for a, b in pairs:
        s.append('<circle cx="%.1f" cy="%.1f" r="2.8" fill="%s" opacity="0.6"/>' % (X(a), Y(b), C_BLUE))
    if fit:
        x0, x1 = xlo, xhi
        y0 = fit["intercept"] + fit["coef"][0] * x0
        y1 = fit["intercept"] + fit["coef"][0] * x1
        s.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" stroke-width="1.8"/>'
                 % (X(x0), Y(y0), X(x1), Y(y1), C_RED))
    if r is not None:
        s.append('<text x="%d" y="%d" font-size="11" fill="%s">r = %.3f   p = %.4g   R² = %.3f   n = %d</text>'
                 % (ml + pw - 260, mt + 12, "#334155", r, p or 0, (r * r), n))
    s.append('<text x="%d" y="%d" font-size="10" fill="%s" text-anchor="middle">%s</text>'
             % (ml + pw // 2, H - 10, C_GRAY, _esc(xlabel)))
    s.append('</svg>')
    return "".join(s)


# ------------------------------------------------------------------ 主效应图

# scan:keep 库能力：DOE 主效应图。当前 DOE 章节以表格式输出方案，未出图；改为图形化呈现时接上。
def main_effects_plot(main_effects, title="因子主效应", width=760, height=320):
    if not main_effects:
        return ""
    nf = len(main_effects)
    W, H = width, height
    ml, mr, mt, mb = 62, 22, 40, 60
    pw, ph = W - ml - mr, H - mt - mb
    allv = [v for m in main_effects for v in m["level_means"].values() if v is not None]
    lo, hi = min(allv), max(allv)
    pad = (hi - lo) * 0.15 or 1
    lo, hi = lo - pad, hi + pad

    def Y(v):
        return mt + ph - (v - lo) / (hi - lo) * ph

    s = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" width="%d" height="%d" font-family="%s">'
         % (W, H, W, H, FONT)]
    s.append('<rect width="%d" height="%d" fill="#ffffff"/>' % (W, H))
    s.append('<text x="%d" y="24" font-size="14" font-weight="600" fill="#0f172a">%s</text>' % (ml, _esc(title)))
    ticks, step = _nice_ticks(lo, hi, 5)
    for t in ticks:
        s.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="%s" stroke-width="1"/>'
                 % (ml, Y(t), ml + pw, Y(t), C_LIGHT))
        s.append('<text x="%d" y="%.1f" font-size="10" fill="%s" text-anchor="end">%s</text>'
                 % (ml - 6, Y(t) + 3, C_GRAY, _fmt(t, step)))
    cw = pw / nf
    palette = [C_BLUE, C_RED, C_GREEN, C_ORANGE, "#7c3aed"]
    for i, m in enumerate(main_effects):
        col = palette[i % len(palette)]
        lv = sorted(m["level_means"].keys())
        pts = [(ml + i * cw + cw * (0.25 + 0.5 * (k / max(1, len(lv) - 1))), Y(m["level_means"][k]))
               for k, kk in zip(lv, range(len(lv)))]
        s.append('<polyline points="%s" fill="none" stroke="%s" stroke-width="1.8"/>'
                 % (" ".join("%.1f,%.1f" % p for p in pts), col))
        for (px, py), k in zip(pts, lv):
            s.append('<circle cx="%.1f" cy="%.1f" r="3.2" fill="%s"/>' % (px, py, col))
            s.append('<text x="%.1f" y="%.1f" font-size="9" fill="%s" text-anchor="middle">L%s</text>'
                     % (px, py + 14, C_GRAY, k))
        s.append('<text x="%.1f" y="%d" font-size="10" fill="%s" text-anchor="middle">%s</text>'
                 % (ml + i * cw + cw / 2, mt + ph + 18, "#334155", _esc(str(m["factor"])[:12])))
        if m.get("contribution_pct") is not None:
            s.append('<text x="%.1f" y="%d" font-size="9" fill="%s" text-anchor="middle">贡献 %.1f%%</text>'
                     % (ml + i * cw + cw / 2, mt + ph + 32, C_GRAY, m["contribution_pct"]))
    s.append('</svg>')
    return "".join(s)

# -*- coding: utf-8 -*-
"""
report.py —— 标准化报告生成

产出两份：
  1) .md   —— 纯文本，便于复制进邮件 / 企业微信 / 8D 系统
  2) .html —— 单文件自包含（SVG 图表内嵌），离线可看、可直接打印为 PDF

固定 12 段结构，保证不同工程师、不同产线的报告可直接横向对比：
  1 结论摘要 / 2 数据与预处理 / 3 工序与参数识别 / 4 SPC 与控制图 /
  5 过程能力 / 6 相关性分析 / 7 不良 Pareto / 8 根因定位与 5Why /
  9 良率损失拆解 / 10 DOE 与参数优化建议 / 11 数据缺口与下一步 /
  12 8D 初步分析（D1-D4）
"""

import os
import io
import datetime


def _fmt(v, nd=4):
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "是" if v else "否"
    if isinstance(v, float):
        if v == 0:
            return "0"
        if abs(v) >= 1e6 or (abs(v) < 1e-4 and v != 0):
            return "%.3e" % v
        return ("%%.%df" % nd) % v
    return str(v)


class Report(object):
    def __init__(self, title, meta=None):
        self.title = title
        self.meta = meta or {}
        self.sections = []

    def section(self, title, level=2):
        # level 会真的生效：8D 的 D1–D8 是 §12 下的子节，必须渲染成 ###，
        # 否则导出 Word/Pandoc 时目录层级会整体塌一级。
        sec = {"title": title, "blocks": [], "level": int(level) if level else 2}
        self.sections.append(sec)
        return sec

    def add(self, sec, btype, **kw):
        sec["blocks"].append(dict(type=btype, **kw))
        return sec

    # ---- 便捷方法 ----
    def text(self, sec, content):
        return self.add(sec, "text", content=content)

    def table(self, sec, headers, rows, caption=None):
        return self.add(sec, "table", headers=headers, rows=rows, caption=caption)

    def kv(self, sec, pairs):
        return self.add(sec, "kv", pairs=pairs)

    def bullets(self, sec, items, ordered=False):
        return self.add(sec, "list", items=items, ordered=ordered)

    def chart(self, sec, svg, caption=None, fname=None):
        return self.add(sec, "svg", svg=svg, caption=caption, fname=fname)

    def callout(self, sec, content, kind="warn"):
        return self.add(sec, "callout", content=content, kind=kind)

    def code(self, sec, content):
        return self.add(sec, "code", content=content)

    # ---------------------------------------------------------------- 渲染
    def render_md(self, chart_dir=None, chart_rel="charts"):
        out = ["# %s" % self.title, ""]
        if self.meta:
            out.append("> " + " ｜ ".join("%s: %s" % (k, v) for k, v in self.meta.items() if v))
            out.append("")
        for sec in self.sections:
            out.append("%s %s" % ("#" * max(2, int(sec.get("level") or 2)), sec["title"]))
            out.append("")
            for b in sec["blocks"]:
                out.extend(self._md_block(b, chart_dir, chart_rel))
                out.append("")
        return "\n".join(out)

    def _md_block(self, b, chart_dir, chart_rel):
        t = b["type"]
        if t == "text":
            return [b["content"]]
        if t == "table":
            lines = []
            if b.get("caption"):
                lines.append("**%s**" % b["caption"])
                lines.append("")
            hs = b["headers"]
            lines.append("| " + " | ".join(str(h) for h in hs) + " |")
            lines.append("|" + "|".join(["---"] * len(hs)) + "|")
            for r in b["rows"]:
                lines.append("| " + " | ".join(_fmt(c) for c in r) + " |")
            return lines
        if t == "kv":
            return ["- **%s**：%s" % (k, _fmt(v)) for k, v in b["pairs"]]
        if t == "list":
            pre = "1. " if b.get("ordered") else "- "
            return ["%s%s" % (pre if not b.get("ordered") else "%d. " % (i + 1), it)
                    for i, it in enumerate(b["items"])]
        if t == "svg":
            if b.get("fname") and chart_dir:
                return ["![%s](%s/%s)" % (b.get("caption") or "chart", chart_rel, b["fname"])]
            return ["> （图表见 HTML 版报告）"]
        if t == "callout":
            return ["> **%s** %s" % ("提示：" if b.get("kind") == "warn" else "说明：", b["content"])]
        if t == "code":
            return ["```", b["content"], "```"]
        return []

    def render_html(self, chart_dir=None):
        css = """
:root{--bd:#e2e8f0;--tx:#0f172a;--mu:#64748b;--bg:#f8fafc;--ac:#2563eb;}
*{box-sizing:border-box}
body{margin:0;padding:32px 28px;background:#fff;color:var(--tx);
 font:14px/1.75 -apple-system,BlinkMacSystemFont,'Segoe UI','Microsoft YaHei',sans-serif;}
.wrap{max-width:980px;margin:0 auto}
h1{font-size:26px;margin:0 0 6px;letter-spacing:-.2px}
.meta{color:var(--mu);font-size:12.5px;border-bottom:2px solid var(--ac);padding-bottom:14px;margin-bottom:26px}
h2{font-size:18px;margin:34px 0 12px;padding-left:10px;border-left:4px solid var(--ac)}
h3{font-size:15px;margin:20px 0 8px;color:#1e293b}
table{border-collapse:collapse;width:100%;margin:10px 0 18px;font-size:13px}
th{background:var(--bg);text-align:left;padding:8px 10px;border:1px solid var(--bd);font-weight:600;white-space:nowrap}
td{padding:7px 10px;border:1px solid var(--bd);vertical-align:top}
tr:nth-child(even) td{background:#fcfdfe}
ul,ol{margin:8px 0 14px;padding-left:22px}
li{margin:4px 0}
.callout{border-left:4px solid #f59e0b;background:#fffbeb;padding:10px 14px;margin:12px 0;border-radius:0 6px 6px 0}
.callout.info{border-color:var(--ac);background:#eff6ff}
.callout.ok{border-color:#16a34a;background:#f0fdf4}
.callout.bad{border-color:#dc2626;background:#fef2f2}
.fig{margin:16px 0 22px;border:1px solid var(--bd);border-radius:8px;overflow:hidden;background:#fff}
.fig svg{display:block;width:100%;height:auto}
.cap{font-size:12px;color:var(--mu);padding:7px 12px;background:var(--bg);border-top:1px solid var(--bd)}
code,pre{font-family:'SFMono-Regular',Consolas,'Liberation Mono',monospace;font-size:12.5px}
pre{background:var(--bg);padding:12px;border-radius:6px;overflow-x:auto;border:1px solid var(--bd)}
.badge{display:inline-block;padding:1px 8px;border-radius:10px;font-size:12px;font-weight:600}
.b-red{background:#fee2e2;color:#991b1b}.b-org{background:#ffedd5;color:#9a3412}
.b-grn{background:#dcfce7;color:#166534}.b-blu{background:#dbeafe;color:#1e40af}
.gry{color:var(--mu)}
.foot{margin-top:40px;padding-top:14px;border-top:1px solid var(--bd);color:var(--mu);font-size:12px}
@media print{body{padding:0}h2{page-break-after:avoid}table,.fig{page-break-inside:avoid}}
"""
        out = ['<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">',
               '<meta name="viewport" content="width=device-width,initial-scale=1">',
               "<title>%s</title><style>%s</style></head><body><div class=\"wrap\">" % (self.title, css)]
        out.append("<h1>%s</h1>" % self.title)
        if self.meta:
            out.append('<div class="meta">%s</div>'
                       % " ｜ ".join("%s: %s" % (k, v) for k, v in self.meta.items() if v))
        for sec in self.sections:
            _lv = max(2, min(6, int(sec.get("level") or 2)))
            out.append("<h%d>%s</h%d>" % (_lv, sec["title"], _lv))
            for b in sec["blocks"]:
                out.append(self._html_block(b, chart_dir))
        out.append('<div class="foot">本报告由 workbuddy skill <b>newenergy-fde</b> 自动生成 ｜ '
                   '生成时间 %s ｜ 结论需结合现场工艺与设备状态复核后执行</div>'
                   % datetime.datetime.now().strftime("%Y-%m-%d %H:%M"))
        out.append("</div></body></html>")
        return "\n".join(out)

    def _html_block(self, b, chart_dir):
        t = b["type"]
        if t == "text":
            return "<p>%s</p>" % b["content"]
        if t == "table":
            h = ["<table>"]
            if b.get("caption"):
                h.append("<caption style='text-align:left;font-weight:600;padding:6px 0;color:#334155'>%s</caption>"
                         % b["caption"])
            h.append("<thead><tr>" + "".join("<th>%s</th>" % c for c in b["headers"]) + "</tr></thead><tbody>")
            for r in b["rows"]:
                h.append("<tr>" + "".join("<td>%s</td>" % _fmt(c) for c in r) + "</tr>")
            h.append("</tbody></table>")
            return "".join(h)
        if t == "kv":
            return "<ul>" + "".join("<li><b>%s</b>：%s</li>" % (k, _fmt(v)) for k, v in b["pairs"]) + "</ul>"
        if t == "list":
            tag = "ol" if b.get("ordered") else "ul"
            return "<%s>%s</%s>" % (tag, "".join("<li>%s</li>" % i for i in b["items"]), tag)
        if t == "svg":
            if not b.get("svg"):
                return ""
            return ('<div class="fig">%s<div class="cap">%s</div></div>'
                    % (b["svg"], b.get("caption") or "图"))
        if t == "callout":
            kind = b.get("kind", "warn")
            return '<div class="callout %s">%s</div>' % (
                {"warn": "", "info": "info", "ok": "ok", "bad": "bad"}.get(kind, ""), b["content"])
        if t == "code":
            return "<pre>%s</pre>" % b["content"].replace("&", "&amp;").replace("<", "&lt;")
        return ""


def badge(text, kind):
    return '<span class="badge b-%s">%s</span>' % (kind, text)


def save_report(rep, outdir, basename, chart_rel="charts"):
    """保存 md/html/svg，返回文件路径 dict"""
    if not os.path.isdir(outdir):
        os.makedirs(outdir)
    cdir = os.path.join(outdir, chart_rel)
    if not os.path.isdir(cdir):
        os.makedirs(cdir)
    saved = []
    for sec in rep.sections:
        for b in sec["blocks"]:
            if b["type"] == "svg" and b.get("svg") and b.get("fname"):
                p = os.path.join(cdir, b["fname"])
                with io.open(p, "w", encoding="utf-8") as f:
                    f.write(b["svg"])
                saved.append(p)
    md = rep.render_md(chart_dir=cdir, chart_rel=chart_rel)
    html = rep.render_html(chart_dir=cdir)
    mp = os.path.join(outdir, basename + ".md")
    hp = os.path.join(outdir, basename + ".html")
    with io.open(mp, "w", encoding="utf-8") as f:
        f.write(md)
    with io.open(hp, "w", encoding="utf-8") as f:
        f.write(html)
    return {"md": mp, "html": hp, "charts": saved, "dir": outdir}

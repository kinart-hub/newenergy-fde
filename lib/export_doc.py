# -*- coding: utf-8 -*-
"""
export_doc.py —— 原生 Word(.docx) 与 PDF 导出（零第三方依赖）

为什么必须自己写：
    报告要进 8D 系统、要发给客户、要走签核流程，而对面要的是 Word 或 PDF。
    以前只有 HTML 可打印 + Markdown，使用者得自己转一遍——每多一道手工转换，
    就多一次"报告版本和实际结论对不上"的机会。

    本项目承诺零第三方依赖，所以不能用 python-docx / reportlab。
    两者都是**开放格式**：docx 是 OOXML（zip 里的 XML），PDF 1.4 是公开规范，
    都能只用标准库 zipfile / zlib / 文本写出。

设计边界（如实说明，不吹）：
    · docx：输出 Word 2016+ 可直接打开的包；图表以 SVG 原样嵌入
      （Word 2016 起原生支持 SVG，矢量放大不糊）。
    · pdf ：文本与表格完整，**不嵌入字体**——中文用 Adobe-GB1 预定义字体
      STSong-Light（PDF 规范允许的非嵌入 CJK 字体，主流阅读器都有）。
      需要"嵌入字体以保真"的场合，请改用 docx 转 PDF。
      图表的 SVG 只做最小子集渲染（直线/折线/矩形/文字），
      复杂路径会在原位置标注为"图见同名 SVG"。

零第三方依赖：只用 os / io / re / zipfile / zlib。
"""

import io
import math
import os
import re
import zlib
import zipfile

try:
    from . import degrade
except Exception:                    # 脚本模式
    import degrade                   # noqa

# ------------------------------------------------------------------ 通用清洗
_TAG_RE = re.compile(r"<[^>]+>")
_MD_BOLD = re.compile(r"\*\*(.+?)\*\*")


def _plain(s):
    """去掉 HTML/Markdown 标记，取纯文本（Word/PDF 里不该出现裸标签）。"""
    s = "" if s is None else str(s)
    s = _MD_BOLD.sub(r"\1", s)
    s = _TAG_RE.sub("", s)
    return (s.replace("&lt;", "<").replace("&gt;", ">")
             .replace("&amp;", "&").replace("&nbsp;", " "))


def _fmt_cell(v):
    if v is None:
        return ""
    if isinstance(v, float):
        # 表格里的浮点不要出现 0.30000000000000004 这种东西
        return ("%.4g" % v).rstrip("0").rstrip(".") if v == v else ""
    return _plain(v)


def _xml_escape(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


# ------------------------------------------------------------------ DOCX
_NS_DOC = (
    'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
    'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture"'
)

_STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:docDefaults><w:rPrDefault><w:rPr>
<w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:eastAsia="SimSun" w:cs="SimSun"/>
<w:sz w:val="21"/></w:rPr></w:rPrDefault></w:docDefaults>
<w:style w:type="paragraph" w:default="1" w:styleId="Normal">
<w:name w:val="Normal"/><w:qFormat/></w:style>
<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/>
<w:basedOn w:val="Normal"/><w:pPr><w:spacing w:after="240"/></w:pPr>
<w:rPr><w:b/><w:sz w:val="40"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/>
<w:basedOn w:val="Normal"/><w:qFormat/><w:pPr><w:spacing w:before="240" w:after="120"/>
<w:outlineLvl w:val="0"/></w:pPr>
<w:rPr><w:b/><w:sz w:val="32"/>
<w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:eastAsia="SimHei"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/>
<w:basedOn w:val="Normal"/><w:qFormat/><w:pPr><w:spacing w:before="200" w:after="100"/>
<w:outlineLvl w:val="1"/></w:pPr>
<w:rPr><w:b/><w:sz w:val="28"/>
<w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:eastAsia="SimHei"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading3"><w:name w:val="heading 3"/>
<w:basedOn w:val="Normal"/><w:qFormat/><w:pPr><w:spacing w:before="160" w:after="80"/>
<w:outlineLvl w:val="2"/></w:pPr>
<w:rPr><w:b/><w:sz w:val="24"/>
<w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:eastAsia="SimHei"/></w:rPr></w:style>
<w:style w:type="table" w:styleId="TableGrid"><w:name w:val="Table Grid"/>
<w:tblPr><w:tblBorders>
<w:top w:val="single" w:sz="4" w:color="999999"/>
<w:left w:val="single" w:sz="4" w:color="999999"/>
<w:bottom w:val="single" w:sz="4" w:color="999999"/>
<w:right w:val="single" w:sz="4" w:color="999999"/>
<w:insideH w:val="single" w:sz="4" w:color="999999"/>
<w:insideV w:val="single" w:sz="4" w:color="999999"/>
</w:tblBorders></w:tblPr></w:style>
</w:styles>"""

_EMU_IN = 914400          # 1 英寸 = 914400 EMU
_PAGE_W = int(8.27 * _EMU_IN)      # A4 纵向宽度（英寸）
_PAGE_H = int(11.69 * _EMU_IN)
_MARGIN = int(0.85 * _EMU_IN)
_USABLE_W = _PAGE_W - 2 * _MARGIN


def _svg_size(svg_text, default_w=640, default_h=360):
    """从 SVG 里读 width/height（或 viewBox），用于按原比例排版图片。"""
    try:
        m = re.search(r'width="([\d.]+)(px)?"', svg_text)
        n = re.search(r'height="([\d.]+)(px)?"', svg_text)
        if m and n:
            return float(m.group(1)), float(n.group(1))
        m = re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', svg_text)
        if m:
            return float(m.group(1)), float(m.group(2))
    except Exception as exc:
        degrade.note("export_doc.svg_size", exc)
    return float(default_w), float(default_h)


def _para_xml(text, style=None, bold=False, size=None, color=None):
    t = _xml_escape(text)
    rpr = []
    if bold:
        rpr.append("<w:b/>")
    if size:
        rpr.append('<w:sz w:val="%d"/><w:szCs w:val="%d"/>' % (size, size))
    if color:
        rpr.append('<w:color w:val="%s"/>' % color)
    ppr = ('<w:pPr><w:pStyle w:val="%s"/></w:pPr>' % style) if style else ""
    rprs = ("<w:rPr>%s</w:rPr>" % "".join(rpr)) if rpr else ""
    return ('<w:p>%s<w:r>%s<w:t xml:space="preserve">%s</w:t></w:r></w:p>'
            % (ppr, rprs, t))


def _table_xml(headers, rows):
    def cell(v, head=False):
        v = _fmt_cell(v)
        shd = '<w:shd w:val="clear" w:fill="EEF2F7"/>' if head else ""
        rpr = "<w:rPr><w:b/></w:rPr>" if head else ""
        return ('<w:tc><w:tcPr><w:tcW w:w="0" w:type="auto"/>%s</w:tcPr>'
                '<w:p><w:r>%s<w:t xml:space="preserve">%s</w:t></w:r></w:p></w:tc>'
                % (shd, rpr, _xml_escape(v)))

    trs = ['<w:tr>%s</w:tr>' % "".join(cell(h, True) for h in (headers or []))]
    for r in (rows or []):
        trs.append('<w:tr>%s</w:tr>' % "".join(cell(c) for c in r))
    return ('<w:tbl><w:tblPr><w:tblStyle w:val="TableGrid"/>'
            '<w:tblW w:w="5000" w:type="pct"/>'
            '<w:tblBorders>'
            '<w:top w:val="single" w:sz="4" w:color="BBBBBB"/>'
            '<w:left w:val="single" w:sz="4" w:color="BBBBBB"/>'
            '<w:bottom w:val="single" w:sz="4" w:color="BBBBBB"/>'
            '<w:right w:val="single" w:sz="4" w:color="BBBBBB"/>'
            '<w:insideH w:val="single" w:sz="4" w:color="BBBBBB"/>'
            '<w:insideV w:val="single" w:sz="4" w:color="BBBBBB"/>'
            '</w:tblBorders></w:tblPr>%s</w:tbl><w:p/>' % "".join(trs))


def _image_xml(rid, cx, cy, name, rid_num):
    return (
        '<w:p><w:r><w:drawing>'
        '<wp:inline distT="0" distB="0" distL="0" distR="0">'
        '<wp:extent cx="%d" cy="%d"/>'
        '<wp:effectExtent l="0" t="0" r="0" b="0"/>'
        '<wp:docPr id="%d" name="%s"/>'
        '<wp:cNvGraphicFramePr/>'
        '<a:graphic>'
        '<a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">'
        '<pic:pic>'
        '<pic:nvPicPr><pic:cNvPr id="0" name="%s"/><pic:cNvPicPr/></pic:nvPicPr>'
        '<pic:blipFill><a:blip r:embed="%s"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>'
        '<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="%d" cy="%d"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr>'
        '</pic:pic></a:graphicData></a:graphic>'
        '</wp:inline></w:drawing></w:r></w:p>'
        % (cx, cy, rid_num, _xml_escape(name), _xml_escape(name), rid, cx, cy))


def render_docx(rep, chart_dir=None):
    """把 Report 渲染成 .docx（OOXML）字节串。返回 bytes。

    图表走 SVG 原样嵌入：栅格化需要图形库（不可能零依赖），
    而 SVG 是矢量，Word 2016 起原生支持，放大不糊——这是这条路的最优解。
    """
    body = []
    rels = []                      # 图片关系项
    parts = {}                     # 图片部件：name -> bytes
    _rid = 100

    if rep.title:
        body.append(_para_xml(rep.title, style="Title"))
    if rep.meta:
        _mv = " ｜ ".join("%s: %s" % (k, v) for k, v in rep.meta.items() if v)
        if _mv:
            body.append(_para_xml(_mv, color="64748B"))

    for sec in rep.sections:
        _lv = int(sec.get("level") or 2)
        _st = "Heading1" if _lv <= 2 else ("Heading2" if _lv == 3 else "Heading3")
        body.append(_para_xml(sec.get("title") or "", style=_st))
        for b in sec["blocks"]:
            _t = b.get("type")
            if _t == "text":
                body.append(_para_xml(_plain(b.get("content"))))
            elif _t == "callout":
                _k = b.get("kind") or "warn"
                _tag = {"bad": "⛔ ", "warn": "⚠ ", "ok": "✅ ", "info": "ℹ "}.get(_k, "ℹ ")
                _col = {"bad": "B91C1C", "warn": "92400E",
                        "ok": "166534", "info": "334155"}.get(_k, "334155")
                body.append(_para_xml(_tag + _plain(b.get("content")),
                                      bold=(_k == "bad"), color=_col))
            elif _t == "kv":
                for k, v in (b.get("pairs") or []):
                    body.append(_para_xml("%s：%s" % (_plain(k), _fmt_cell(v))))
            elif _t == "list":
                for i, it in enumerate(b.get("items") or []):
                    body.append(_para_xml(("  " if i else "· ") + _plain(it)))
            elif _t == "table":
                if b.get("caption"):
                    body.append(_para_xml(_plain(b["caption"]), bold=True, size=19))
                body.append(_table_xml(b.get("headers"), b.get("rows")))
            elif _t == "code":
                body.append(_para_xml(_plain(b.get("content")), size=19))
            elif _t == "svg":
                _svg = b.get("svg") or ""
                _fn = b.get("fname")
                if b.get("caption"):
                    body.append(_para_xml(_plain(b["caption"]), bold=True, size=19))
                if not _svg and not _fn:
                    continue
                _data = _svg.encode("utf-8")
                if not _data and _fn and chart_dir:
                    _p = os.path.join(chart_dir, _fn)
                    if os.path.isfile(_p):
                        with io.open(_p, "rb") as fh:
                            _data = fh.read()
                if not _data:
                    continue
                _w, _h = _svg_size(_svg or "<svg/>")
                _scale = _USABLE_W / float(max(_w, 1.0))
                cx = int(min(_USABLE_W, _w * _scale))
                cy = int(_h * _scale)
                _rid += 1
                _name = "chart%d.svg" % _rid
                parts[_name] = _data
                rels.append(("rId%d" % _rid, "image", "media/" + _name))
                body.append(_image_xml("rId%d" % _rid, cx, cy, _name, _rid))
                if _fn:
                    body.append(_para_xml("（矢量图，同目录 %s）" % _fn, size=17,
                                          color="64748B"))

    body.append(
        '<w:sectPr><w:pgSz w:w="%d" w:h="%d"/>'
        '<w:pgMar w:top="%d" w:right="%d" w:bottom="%d" w:left="%d"/>'
        '</w:sectPr>' % (_PAGE_W, _PAGE_H, _MARGIN, _MARGIN, _MARGIN, _MARGIN))

    doc = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<w:document %s><w:body>%s</w:body></w:document>' % (_NS_DOC, "".join(body)))

    ct = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
          '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">',
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
          '<Default Extension="xml" ContentType="application/xml"/>',
          '<Default Extension="svg" ContentType="image/svg+xml"/>',
          '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>',
          '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>']
    # 注意：同一个扩展名的 <Default> 只能出现一次。
    # 按"每张图加一条"的写法虽然也能被 Word 容忍，但那是靠对方的容错，
    # 不是规范——重复的 Default 在严格解析器里就是坏包。
    ct.append('</Types>')

    root_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                 '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                 '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
                 '</Relationships>')

    doc_rels = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">',
                '<Relationship Id="rId50" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>']
    for rid, _kind, target in rels:
        doc_rels.append('<Relationship Id="%s" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="%s"/>'
                        % (rid, target))
    doc_rels.append('</Relationships>')

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", "".join(ct))
        z.writestr("_rels/.rels", root_rels)
        z.writestr("word/document.xml", doc)
        z.writestr("word/styles.xml", _STYLES)
        z.writestr("word/_rels/document.xml.rels", "".join(doc_rels))
        for n, d in parts.items():
            z.writestr("word/media/" + n, d)
    return buf.getvalue()


def write_docx(rep, path, chart_dir=None):
    data = render_docx(rep, chart_dir=chart_dir)
    _d = os.path.dirname(path)
    if _d and not os.path.isdir(_d):
        os.makedirs(_d)
    with io.open(path, "wb") as f:
        f.write(data)
    return path


# ------------------------------------------------------------------ PDF
# 版面常量（A4 纵向，单位 pt）
_PDF_W, _PDF_H = 595.28, 841.89
_M = 48.0                       # 页边距
_USABLE = _PDF_W - 2 * _M
_FS_BODY, _FS_H1, _FS_H2, _FS_H3 = 10.5, 16.0, 13.0, 11.5
_LH = 1.45                      # 行高倍数


def _char_w(ch, fs):
    """字符宽度估算：CJK 近似 1.0 em，ASCII 近似 0.52 em。

    不嵌入字体就没有精确的 AFM 宽度表，只能估算。估算偏宽一点更安全：
    宁可多换一行，也不要把字挤出页边。
    """
    o = ord(ch)
    if o > 0x2E80:              # CJK / 全角标点
        return fs
    if ch in "ilj|.,;:'!":
        return fs * 0.30
    if ch in "ftr()[]{}":
        return fs * 0.36
    if ch.isupper() or ch in "mwMW@%":
        return fs * 0.62
    return fs * 0.52


def _wrap(text, fs, max_w):
    """按估算宽度折行。中文逐字断行（本来就没有词间空格）。"""
    lines, cur, w = [], [], 0.0
    for ch in (text or ""):
        if ch == "\n":
            lines.append("".join(cur))
            cur, w = [], 0.0
            continue
        cw = _char_w(ch, fs)
        if w + cw > max_w and cur:
            lines.append("".join(cur))
            cur, w = [], 0.0
        cur.append(ch)
        w += cw
    if cur:
        lines.append("".join(cur))
    return lines or [""]


def _utf16be_hex(s):
    """文本转 UTF-16BE 十六进制串。

    走十六进制串（<...>）而不是字面串（(...)）：后者只对 WinAnsi 有效，
    中文要靠 Type0 + UniGB-UCS2-H 才能取到字形，而字面串里的非 ASCII 字节
    会被当成 WinAnsi 编码，出来就是乱码。

    非 BMP 字符（>U+FFFF，如部分 emoji）必须拆成代理对：直接 "%04X" 会写出
    5~6 位十六进制，UTF-16 的两个字节就此错位，后面整段文字都会变成乱码方块。
    """
    out = []
    for c in s:
        o = ord(c)
        if o > 0xFFFF:
            o -= 0x10000
            out.append("%04X" % (0xD800 + (o >> 10)))
            out.append("%04X" % (0xDC00 + (o & 0x3FF)))
        else:
            out.append("%04X" % o)
    return "".join(out)


def _pdf_text(x, y, s, fs, font="F1", color=(0.1, 0.1, 0.12)):
    """WinAnsi 只够西文；中文走 Type0 + UniGB-UCS2-H，文本要用 UTF-16BE 十六进制串。"""
    return ("BT /%s %.2f Tf %.3f %.3f %.3f rg 1 0 0 1 %.2f %.2f Tm <%s> Tj ET\n"
            % (font, fs, color[0], color[1], color[2], x, y, _utf16be_hex(s)))


class _Page(object):
    def __init__(self):
        self.ops = []

    def text(self, x, y, s, fs, color=(0.1, 0.1, 0.12)):
        for i, ln in enumerate(_wrap(s, fs, _USABLE)):
            self.ops.append(_pdf_text(x, y - i * fs * _LH, ln, fs, color=color))
        return y - len(_wrap(s, fs, _USABLE)) * fs * _LH

    def line(self, x1, y1, x2, y2, w=0.6, color=(0.6, 0.6, 0.62)):
        self.ops.append("%.3f %.3f %.3f RG %.2f w %.2f %.2f m %.2f %.2f l S\n"
                        % (color[0], color[1], color[2], w, x1, y1, x2, y2))

    def rect(self, x, y, w, h, color=(0.85, 0.85, 0.87)):
        self.ops.append("%.3f %.3f %.3f rg %.2f %.2f %.2f %.2f re f\n"
                        % (color[0], color[1], color[2], x, y, w, h))

    def poly(self, pts, color=(0.15, 0.35, 0.75), w=1.0):
        if not pts:
            return
        self.ops.append("%.3f %.3f %.3f RG %.2f w " % (color[0], color[1], color[2], w))
        self.ops.append("%.2f %.2f m " % pts[0])
        for p in pts[1:]:
            self.ops.append("%.2f %.2f l " % p)
        self.ops.append("S\n")

    def stream(self):
        return "".join(self.ops)


def _svg_to_ops(pg, svg, x, y, w, h):
    """最小 SVG 子集 → PDF 绘图指令。返回 (能不能画, 有几个元素没画出来)。

    只认 line / rect / polyline / polygon / circle / text 这几种我们自己
    画图表会用到的元素。认不出的（复杂 path、渐变、裁剪）不硬猜：
    在原位置标注"图见同名 SVG"——画一张错的图比不画更糟。

    第二个返回值是"部分转换"的信号，不能省：一张折线图少画了几个点，
    图照样出得来，读者却看不出少。静默跳过等于交出一张看着完整、
    实际缺数据的图——这比整张画不出来更容易误导判断。
    """
    if not svg:
        return False, 0
    _vb = re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', svg)
    _sw = re.search(r'width="([\d.]+)', svg)
    _sh = re.search(r'height="([\d.]+)', svg)
    vw = float(_vb.group(1)) if _vb else (float(_sw.group(1)) if _sw else 640.0)
    vh = float(_vb.group(2)) if _vb else (float(_sh.group(1)) if _sh else 360.0)
    if vw <= 0 or vh <= 0:
        return False
    sx, sy = w / vw, h / vh

    def tx(vx):
        return x + vx * sx

    def ty(vy):
        # SVG 的 y 向下，PDF 的 y 向上
        return y + h - vy * sy

    n = 0
    skipped = 0
    for m in re.finditer(r"<line\b([^>]*)/?>", svg):
        a = m.group(1)
        g = lambda k, d=0.0: float((re.search(k + r'="([-\d.]+)"', a) or
                                    [None, str(d)])[1])
        pg.poly([(tx(g("x1")), ty(g("y1"))), (tx(g("x2")), ty(g("y2")))],
                color=(0.75, 0.78, 0.82), w=0.5)
        n += 1
    for m in re.finditer(r"<(?:rect|circle|polyline|polygon)\b([^>]*)/?>", svg):
        a = m.group(1)
        tag = m.group(0)
        if tag.startswith("<rect"):
            g = lambda k, d=0.0: float((re.search(k + r'="([-\d.]+)"', a) or
                                        [None, str(d)])[1])
            _x0, _y0 = tx(g("x")), ty(g("y") + g("height"))
            pg.rect(_x0, _y0, g("width") * sx, g("height") * sy,
                    color=(0.72, 0.80, 0.92))
            n += 1
        elif tag.startswith("<circle"):
            cx = float((re.search(r'cx="([-\d.]+)"', a) or [None, "0"])[1])
            cy = float((re.search(r'cy="([-\d.]+)"', a) or [None, "0"])[1])
            r = float((re.search(r'r="([-\d.]+)"', a) or [None, "2"])[1])
            # 圆用八边形近似：PDF 的贝塞尔曲线这里用不上，画个多边形足够识别
            pts = [(tx(cx + r * math.cos(2 * math.pi * i / 8.0)),
                    ty(cy + r * math.sin(2 * math.pi * i / 8.0))) for i in range(9)]
            pg.poly(pts, color=(0.85, 0.35, 0.35), w=0.8)
            n += 1
        else:
            pts_s = (re.search(r'points="([^"]+)"', a) or [None, ""])[1]
            pts = []
            for pair in pts_s.split():
                if "," in pair:
                    _a, _b = pair.split(",", 1)
                    try:
                        pts.append((tx(float(_a)), ty(float(_b))))
                    except ValueError:
                        # 坐标解析不出来就少一个点，但必须计数交给调用方出声：
                        # 折线图少画几个点，图照样出得来，读者看不出少了
                        skipped += 1
            if len(pts) >= 2:
                pg.poly(pts, color=(0.15, 0.35, 0.75), w=1.0)
                n += 1
            elif skipped:
                pass  # 一个点都没成：下面按"整张画不出来"处理
    for m in re.finditer(r"<text\b([^>]*)>([^<]*)</text>", svg):
        a, txt = m.group(1), m.group(2)
        if not txt.strip():
            continue
        _x = float((re.search(r'x="([-\d.]+)"', a) or [None, "0"])[1])
        _y = float((re.search(r'y="([-\d.]+)"', a) or [None, "0"])[1])
        _fs = float((re.search(r'font-size="([-\d.]+)"', a) or [None, "12"])[1])
        pg.ops.append(_pdf_text(tx(_x), ty(_y), txt, max(6.0, _fs * sy * 0.9)))
        n += 1
    return n > 0, skipped


def render_pdf(rep):
    """把 Report 渲染成 PDF 1.4 字节串（不嵌入字体）。

    中文用 Adobe-GB1 预定义字体 STSong-Light（/Encoding /UniGB-UCS2-H）。
    这是 PDF 规范允许的**非嵌入** CJK 字体：主流阅读器（Acrobat、系统预览、
    浏览器内置 PDF）都带或能合成。若你的交付要求字体嵌入保真，
    请用导出的 .docx 转 PDF——那才是零依赖下唯一能保真的路径。
    """
    pages, pg = [], _Page()
    y = _PDF_H - _M

    def new_page():
        nonlocal pg, y
        pages.append(pg)
        pg = _Page()
        y = _PDF_H - _M

    def need(h):
        nonlocal y
        if y - h < _M + 24:
            new_page()
            return True
        return False

    if rep.title:
        need(_FS_H1 * 2)
        y = pg.text(_M, y, rep.title, _FS_H1 + 2) - 4
        pg.line(_M, y + 6, _PDF_W - _M, y + 6, w=0.8, color=(0.3, 0.4, 0.6))
        y -= 10
    if rep.meta:
        _mv = " ｜ ".join("%s: %s" % (k, v) for k, v in rep.meta.items() if v)
        if _mv:
            y = pg.text(_M, y, _mv, _FS_BODY - 1, color=(0.35, 0.38, 0.42)) - 6

    for sec in rep.sections:
        _lv = int(sec.get("level") or 2)
        fs = _FS_H1 if _lv <= 2 else (_FS_H2 if _lv == 3 else _FS_H3)
        need(fs * 3)
        y = pg.text(_M, y - 6, sec.get("title") or "", fs) - 4
        for b in sec["blocks"]:
            _t = b.get("type")
            if _t == "text":
                need(20)
                y = pg.text(_M, y, _plain(b.get("content")), _FS_BODY) - 3
            elif _t == "callout":
                _k = b.get("kind") or "warn"
                _txt = {"bad": "⛔ ", "warn": "⚠ ", "ok": "✅ ",
                        "info": "ℹ "}.get(_k, "ℹ ") + _plain(b.get("content"))
                _col = ((0.72, 0.11, 0.11) if _k == "bad" else
                        (0.57, 0.35, 0.05) if _k == "warn" else
                        (0.09, 0.40, 0.20) if _k == "ok" else (0.20, 0.24, 0.33))
                need(30)
                y = pg.text(_M + 6, y, _txt, _FS_BODY, color=_col) - 4
            elif _t == "kv":
                for k, v in (b.get("pairs") or []):
                    need(18)
                    y = pg.text(_M + 6, y, "%s：%s" % (_plain(k), _fmt_cell(v)),
                                _FS_BODY) - 2
            elif _t == "list":
                for it in (b.get("items") or []):
                    need(18)
                    y = pg.text(_M + 10, y, "· " + _plain(it), _FS_BODY) - 2
            elif _t == "table":
                hdrs = [_fmt_cell(h) for h in (b.get("headers") or [])]
                rows = [[_fmt_cell(c) for c in r] for r in (b.get("rows") or [])]
                if b.get("caption"):
                    need(20)
                    y = pg.text(_M, y, _plain(b["caption"]), _FS_BODY) - 2
                if not hdrs:
                    continue
                ncol = max(1, len(hdrs))
                cw = _USABLE / float(ncol)
                rh = _FS_BODY * _LH + 3

                def draw_row(cells, head=False, yy=0.0):
                    if head:
                        pg.rect(_M, yy - 2, _USABLE, rh, color=(0.90, 0.93, 0.97))
                    for i in range(ncol):
                        _cx = _M + i * cw
                        pg.line(_cx, yy - 2, _cx, yy - 2 + rh, w=0.4,
                                color=(0.75, 0.77, 0.80))
                        _txt = cells[i] if i < len(cells) else ""
                        _fs = _FS_BODY - 0.5
                        for j, ln in enumerate(_wrap(_txt, _fs, cw - 6)):
                            pg.ops.append(_pdf_text(_cx + 3,
                                                    yy + rh - 4 - _fs - j * _fs * _LH,
                                                    ln, _fs))
                    pg.line(_M, yy - 2, _M + _USABLE, yy - 2, w=0.4,
                            color=(0.75, 0.77, 0.80))
                    pg.line(_M, yy - 2 + rh, _M + _USABLE, yy - 2 + rh, w=0.4,
                            color=(0.75, 0.77, 0.80))

                need(rh * 2.2)
                if y - rh < _M + 24:
                    new_page()
                draw_row(hdrs, head=True, yy=y - rh)
                y -= rh
                for r in rows:
                    if y - rh < _M + 24:
                        new_page()
                        draw_row(hdrs, head=True, yy=y - rh)
                        y -= rh
                    draw_row(r, yy=y - rh)
                    y -= rh
                y -= 6
            elif _t == "code":
                need(20)
                y = pg.text(_M + 6, y, _plain(b.get("content")),
                            _FS_BODY - 1, color=(0.25, 0.28, 0.32)) - 3
            elif _t == "svg":
                _svg = b.get("svg") or ""
                if b.get("caption"):
                    need(20)
                    y = pg.text(_M, y, _plain(b["caption"]), _FS_BODY) - 2
                if not _svg:
                    continue
                _w, _h = _svg_size(_svg)
                _pw = _USABLE
                _ph = _pw * (_h / float(max(_w, 1.0)))
                if _ph > 380:
                    _ph = 380.0
                if y - _ph < _M + 24:
                    new_page()
                _ok, _skipped = _svg_to_ops(pg, _svg, _M, y - _ph, _pw, _ph)
                if not _ok:
                    pg.rect(_M, y - _ph, _pw, _ph, color=(0.94, 0.95, 0.96))
                    pg.ops.append(_pdf_text(
                        _M + 8, y - _ph / 2,
                        "（该图的 SVG 含本导出器不支持的元素，请见同目录 %s）"
                        % (b.get("fname") or "同名 SVG"), _FS_BODY - 1,
                        color=(0.45, 0.45, 0.48)))
                elif _skipped:
                    # 画出来了但不完整：图上少几个点，读者根本看不出来，
                    # 必须明写——"看着完整的图"比"明说画不出来"更容易误导判断
                    pg.ops.append(_pdf_text(
                        _M + 2, y - _ph + 10,
                        "（PDF 版有 %d 个图元未转换，完整图请见同目录 %s）"
                        % (_skipped, b.get("fname") or "同名 SVG"),
                        _FS_BODY - 2, color=(0.72, 0.35, 0.05)))
                y -= _ph + 6
    pages.append(pg)

    # ---------------- 组装 PDF 对象 ----------------
    objs = []

    def add(body):
        objs.append(body)
        return len(objs)          # 1-based 对象号

    # Pages 对象先占个位，等 Page 对象号都出来后回填 Kids
    pages_slot = add("")
    content_ids = []
    for p in pages:
        _raw = p.stream().encode("utf-8")
        try:
            _data = zlib.compress(_raw, 9)
            _filt = "/Filter /FlateDecode "
        except Exception as exc:
            degrade.note("export_doc.zlib", exc)
            _data, _filt = _raw, ""
        content_ids.append(add(
            ("<< /Length %d %s>>\nstream\n" % (len(_data), _filt)).encode("latin-1")
            + _data + b"\nendstream"))

    fdesc = add("<< /Type /FontDescriptor /FontName /STSong-Light /Flags 4 "
                "/FontBBox [-25 -254 1000 880] /ItalicAngle 0 /Ascent 880 "
                "/Descent -254 /CapHeight 880 /StemV 58 >>")
    cidf = add("<< /Type /Font /Subtype /CIDFont /BaseFont /STSong-Light "
               "/CIDSystemInfo << /Registry (Adobe) /Ordering (GB1) "
               "/Supplement 2 >> /FontDescriptor %d 0 R /DW 1000 >>" % fdesc)
    font_id = add("<< /Type /Font /Subtype /Type0 /BaseFont /STSong-Light "
                  "/Encoding /UniGB-UCS2-H /DescendantFonts [%d 0 R] >>" % cidf)

    page_ids = []
    for _cid in content_ids:
        page_ids.append(add(
            "<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %.2f %.2f] "
            "/Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>"
            % (pages_slot, _PDF_W, _PDF_H, font_id, _cid)))
    objs[pages_slot - 1] = ("<< /Type /Pages /Count %d /Kids [%s] >>"
                            % (len(page_ids),
                               " ".join("%d 0 R" % i for i in page_ids)))
    cat = add("<< /Type /Catalog /Pages %d 0 R >>" % pages_slot)

    out = io.BytesIO()
    out.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for i, body in enumerate(objs, start=1):
        offsets.append(out.tell())
        if isinstance(body, bytes):
            out.write(("%d 0 obj\n" % i).encode("latin-1"))
            out.write(body)
            out.write(b"\nendobj\n")
        else:
            out.write(("%d 0 obj\n%s\nendobj\n" % (i, body)).encode("latin-1"))
    xref_at = out.tell()
    n = len(objs) + 1
    out.write(("xref\n0 %d\n" % n).encode("latin-1"))
    out.write(b"0000000000 65535 f \n")
    for i in range(1, n):
        out.write(("%010d 00000 n \n" % offsets[i]).encode("latin-1"))
    out.write(("trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n"
               % (n, cat, xref_at)).encode("latin-1"))
    cat = cat  # noqa: 保持引用清晰（Catalog 对象号在上面已登记）
    return out.getvalue()


def write_pdf(rep, path):
    data = render_pdf(rep)
    _d = os.path.dirname(path)
    if _d and not os.path.isdir(_d):
        os.makedirs(_d)
    with io.open(path, "wb") as f:
        f.write(data)
    return path

# -*- coding: utf-8 -*-
"""
table.py —— 零依赖表格引擎（CSV / XLSX 读写）

设计约束：不使用 pandas / openpyxl / numpy。
  - CSV  : 标准库 csv
  - XLSX 读: zipfile + xml.etree（解析 sharedStrings / worksheet / styles 日期判定）
  - XLSX 写: zipfile + 最小 OOXML 结构（inlineStr）
这样 Skill 可在任意 Python 3.8+ 环境运行，无需 pip install。
"""

from . import degrade
import csv
import io
import os
import re
import zipfile
import datetime
import xml.etree.ElementTree as ET

# ---------------------------------------------------------------- 通用工具

NS_MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
NS_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"
NS_DOCREL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"

# Excel 内置日期格式 id
_BUILTIN_DATE_FMT = set(range(14, 23)) | {27, 30, 36, 45, 46, 47, 50, 57}


def _cell_ref_to_xy(ref):
    """A1 -> (0,0) 列索引, 行索引"""
    m = re.match(r"([A-Z]+)(\d+)", ref or "")
    if not m:
        return 0, 0
    col_s, row_s = m.group(1), m.group(2)
    col = 0
    for ch in col_s:
        col = col * 26 + (ord(ch) - 64)
    return col - 1, int(row_s) - 1


def _excel_serial_to_datetime(serial):
    """Excel 序列号 -> datetime（1900 日期系统，含 1900 闰年 bug 修正）"""
    try:
        serial = float(serial)
    except (TypeError, ValueError):
        return None
    if serial <= 0 or serial > 80000:
        return None
    if serial < 60:
        base = datetime.datetime(1899, 12, 31)
    else:
        base = datetime.datetime(1899, 12, 30)
    try:
        return base + datetime.timedelta(days=serial)
    except OverflowError:
        return None


_DATE_PATTERNS = [
    (re.compile(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}[ T]\d{1,2}:\d{2}(:\d{2})?$"), None),
    (re.compile(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}$"), None),
    (re.compile(r"^\d{4}/\d{1,2}/\d{1,2} \d{1,2}:\d{2}$"), None),
    (re.compile(r"^\d{4}\d{2}\d{2}$"), "%Y%m%d"),
    (re.compile(r"^\d{4}\d{2}\d{2} \d{2}:\d{2}(:\d{2})?$"), None),
    (re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"), None),
]


def parse_datetime(value):
    """尽力把各种时间写法解析为 datetime，失败返回 None"""
    if isinstance(value, datetime.datetime):
        return value
    if isinstance(value, datetime.date):
        return datetime.datetime(value.year, value.month, value.day)
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    for pat, fmt in _DATE_PATTERNS:
        if pat.match(s):
            if fmt:
                try:
                    return datetime.datetime.strptime(s, fmt)
                except ValueError:
                    return None
            for f in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
                      "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d",
                      "%Y%m%d %H:%M:%S", "%Y%m%d %H:%M", "%Y-%m-%dT%H:%M:%S"):
                try:
                    return datetime.datetime.strptime(s, f)
                # scan:keep 探测型容错：逐个日期格式试解是正常路径（"值不是日期"是
                # 本函数的合法返回），这里每个单元格都被调用一次，逐次记账会把台账
                # 刷爆；与同一文件 to_number@L125 的豁免同一理由，不是降级。
                except ValueError:
                    continue
            return None
    # 纯数字的 Excel 日期序列号
    if re.match(r"^\d+(\.\d+)?$", s):
        return _excel_serial_to_datetime(s)
    return None


_NUM_HEAD_RE = re.compile(r"^[+-]?\d*\.?\d+(?:[eE][+-]?\d+)?")
_NUM_NULLS = frozenset(("na", "n/a", "nan", "null", "none", "-", "--", "/"))


def to_number(value):
    """把单元格值尽力转为 float，失败返回 None

    这是全流水线调用最频繁的函数（20000 行 × 16 列的分析中被调用过 1800 万次），
    所以先走 float() 直通路径，只有带单位后缀（123.4g/m2、45%、3.5N）时才回退到正则。
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if s == "" or s.lower() in _NUM_NULLS:
        return None
    if "," in s:
        s = s.replace(",", "")
    # 直通：绝大多数测量值是纯数字，不用进正则。
    # 这里转失败是**预期路径**（走下面的正则提取"12.3mm"这类带单位的值），
    # 不是降级——这个函数每秒被调用上千次，记账会把台账刷爆。
    try:
        return float(s)
    except Exception:
        pass
        # scan:keep 探测型容错：to_number 每秒被调用上千次，记账会把台账
        # 刷爆；解析不出数字是本函数的正常返回路径（走下面的带单位提取），
        # 不是降级。同类探测型容错都不记账。
    m = _NUM_HEAD_RE.match(s)
    if m is None:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def looks_numeric(values, threshold=0.7):
    """一列里有多大比例能解析为数字"""
    vals = [v for v in values if v not in (None, "")]
    if not vals:
        return False
    ok = sum(1 for v in vals if to_number(v) is not None)
    return ok / len(vals) >= threshold


# ---------------------------------------------------------------- Table 类

class Table(object):
    """轻量列式表：columns(list[str]) + rows(list[list])"""

    def __init__(self, columns, rows, name="sheet1", source_path=None, src_rows=None):
        self.columns = list(columns)
        self.rows = [list(r) for r in rows]
        self.name = name
        self.source_path = source_path
        # 行级追溯：src_rows[i] = 第 i 行在原始文件里的行号（1 基，表头占第 1 行）
        self.src_rows = list(src_rows) if src_rows is not None else None
        # ------------------------------------------------------------------
        # 列解析缓存。
        # 实测：20000 行 × 16 列的数据，to_number 被调用 1828 万次（每个单元格平均 57 次），
        # 因为 numeric_columns() / numeric_column() / text_columns() 每次被调用都会
        # 重新扫描整列。真正的统计计算只占总耗时的一小部分，绝大部分耗在重复的类型推断上。
        # 这里按「表版本号」缓存：只要 rows/columns 没变，同一列不重复解析。
        # 版本号在 __setattr__ 与所有变更方法（含原地 append 的 add_column）里自增，
        # 因此不会因为缓存而读到脏数据。
        self.__dict__["_rev"] = 0
        self.__dict__["_cache"] = {}

    def _touch(self):
        """表结构或数据发生变化：作废全部列解析缓存"""
        self.__dict__["_rev"] = self.__dict__.get("_rev", 0) + 1
        self._cache.clear()

    def __setattr__(self, key, value):
        # 外部直接 tb.rows = [...] / tb.columns = [...] 时也要作废缓存
        if key in ("rows", "columns", "src_rows") and "_cache" in self.__dict__:
            self._touch()
        object.__setattr__(self, key, value)

    # ---- 基础 ----
    def __len__(self):
        return len(self.rows)

    def shape(self):
        return (len(self.rows), len(self.columns))

    def col_index(self, name):
        try:
            return self.columns.index(name)
        except ValueError:
            return None

    def _cached(self, key, builder):
        ck = self._cache
        ent = ck.get(key)
        rev = self.__dict__.get("_rev", 0)
        if ent is not None and ent[0] == rev:
            return ent[1]
        val = builder()
        ck[key] = (rev, val)
        return val

    def column(self, name):
        """返回原始列值 list（带缓存）"""
        i = self.col_index(name)
        if i is None:
            raise KeyError("列不存在: %s" % name)
        return self._cached(("col", i), lambda: [r[i] if i < len(r) else None
                                                 for r in self.rows])

    def numeric_column(self, name):
        return self._cached(("num", name),
                            lambda: [to_number(v) for v in self.column(name)])

    def numeric_columns(self):
        return self._cached(
            "ncols",
            lambda: [c for c in self.columns
                     if looks_numeric(self.column(c))])

    def text_columns(self):
        return self._cached(
            "tcols",
            lambda: [c for c in self.columns if c not in self.numeric_columns()])

    def rename(self, mapping):
        self.columns = [mapping.get(c, c) for c in self.columns]

    def add_column(self, name, values):
        # columns.append / row.append 是原地修改，绕不过 __setattr__，必须显式作废缓存
        self.columns.append(name)
        for i, r in enumerate(self.rows):
            r.append(values[i] if i < len(values) else None)
        self._touch()

    def drop_columns(self, names):
        keep = [i for i, c in enumerate(self.columns) if c not in names]
        self.columns = [self.columns[i] for i in keep]
        self.rows = [[r[i] if i < len(r) else None for i in keep] for r in self.rows]

    def head(self, n=5):
        return Table(self.columns, self.rows[:n], self.name)

    # ---- 行级追溯 ----
    def attach_source(self, path=None):
        """建立行级追溯：记下每行在原始文件里的行号。
        质量审计要能顺着结论回到原始文件的具体行——没有这一层，
        报告里的"第 37 点异常"在客户那边是没法核对的。"""
        if path:
            self.source_path = path
        self.src_rows = [i + 2 for i in range(len(self.rows))]
        return self

    def src_file(self):
        return os.path.basename(self.source_path) if self.source_path else None

    def src_of(self, i):
        """(文件名, 原始行号)；无追溯信息时行号为 None。"""
        f = self.src_file()
        if self.src_rows and 0 <= i < len(self.src_rows):
            return (f, self.src_rows[i])
        return (f, None)

    def keep_mask(self, mask, reason=None, log=None):
        """按布尔 mask 过滤行，同步追溯链；被剔除的行写进 log。
        清洗必须以"标记"为主——删掉的行全部留痕，客户才能复核是不是误删了真实异常。"""
        if len(mask) != len(self.rows):
            mask = [mask[i] if i < len(mask) else True for i in range(len(self.rows))]
        if log is not None and reason:
            for i, m in enumerate(mask):
                if not m:
                    log.append({
                        "file": self.src_file(),
                        "row": (self.src_rows[i] if self.src_rows else i + 2),
                        "reason": reason,
                        "values": [str(v) for v in self.rows[i][:6]],
                    })
        if self.src_rows and len(self.src_rows) == len(mask):
            self.src_rows = [s for s, m in zip(self.src_rows, mask) if m]
        self.rows = [r for r, m in zip(self.rows, mask) if m]
        return self

    def __repr__(self):
        return "<Table %s %dx%d>" % (self.name, len(self.rows), len(self.columns))


# ---------------------------------------------------------------- 读取 CSV

def read_csv(path, encoding=None, delimiter=None):
    if encoding is None:
        encoding = detect_encoding(path)
    if delimiter is None:
        delimiter = detect_delimiter(path, encoding)
    with io.open(path, "r", encoding=encoding, newline="") as f:
        sample = f.read(8192)
        f.seek(0)
        # 跳过文件顶部的说明行（很多检测设备导出带若干行 header 注释）
        lines = f.readlines()
    if not lines:
        return Table([], [], os.path.basename(path), path)
    start = _find_header_line(lines, delimiter)
    reader = csv.reader(lines[start:], delimiter=delimiter)
    rows = [r for r in reader]
    if not rows:
        return Table([], [], os.path.basename(path), path)
    columns = _dedup([str(c).strip() for c in rows[0]])
    data = []
    for r in rows[1:]:
        if not any((str(x).strip() for x in r)):
            continue
        r = list(r) + [None] * (len(columns) - len(r))
        data.append([x.strip() if isinstance(x, str) else x for x in r[:len(columns)]])
    return Table(columns, data, os.path.splitext(os.path.basename(path))[0], path)


def detect_encoding(path):
    """优先 utf-8-sig / utf-8，其次 gbk（国内 MES 导出常见）"""
    for enc in ("utf-8-sig", "utf-8", "gbk", "gb18030", "latin-1"):
        try:
            with io.open(path, "r", encoding=enc) as f:
                f.read(65536)
            return enc
        # scan:keep 探测型容错：逐个编码试解是正常路径；末位 latin-1 能解任意字节流，
        # 所以"全失败、返回 utf-8 兜底"实际不可达。文件打不开属于 UnicodeDecodeError/
        # LookupError 之外的错误（如权限），由调用侧 read_csv 自行报错并指向真原因。
        except (UnicodeDecodeError, LookupError):
            continue
    return "utf-8"


def detect_delimiter(path, encoding="utf-8"):
    with io.open(path, "r", encoding=encoding, errors="ignore") as f:
        head = "".join([f.readline() for _ in range(5)])
    candidates = [",", "\t", ";", "|"]
    best, best_n = ",", 0
    for d in candidates:
        n = head.count(d)
        if n > best_n:
            best, best_n = d, n
    return best


def _find_header_line(lines, delimiter):
    """定位真正的表头行。

    坑：很多设备导出的前置元信息行本身就带逗号，例如
        "KEYENCE LK-G3000 Series, Controller Firmware Ver.1.23"
    若只按「含分隔符 + 非数值」判断，会把它错认成表头，
    后面整张表的列名和行数全错。

    正确判据是**列数稳定性**：真正的表头会让后续大多数行保持相同的列数，
    而元信息行之后列数会突变（通常是变多）。
    """
    max_i = min(len(lines), 30)
    best_i, best_score = 0, -1e9
    for i in range(max_i):
        line = lines[i]
        if not line.strip():
            continue
        if re.match(r"^\s*[;＃#]", line):
            continue
        cells = [c.strip() for c in line.split(delimiter)]
        ncol = len(cells)
        if ncol < 2:
            continue
        nonnum = sum(1 for c in cells if c and to_number(c) is None)
        if nonnum < max(1, ncol // 2):
            continue

        # 后续行的列数一致度：一致度高才是真表头
        follow = [l for l in lines[i + 1:i + 8] if l.strip()]
        if not follow:
            continue
        consist = sum(1 for l in follow
                      if len(l.split(delimiter)) == ncol) / float(len(follow))

        score = consist * 10.0 + ncol * 0.5 + nonnum / float(ncol)
        if score > best_score:
            best_score, best_i = score, i
    return best_i


def _dedup(names):
    seen = {}
    out = []
    for n in names:
        if n == "" or n is None:
            n = "col_%d" % (len(out) + 1)
        if n in seen:
            seen[n] += 1
            n = "%s_%d" % (n, seen[n])
        else:
            seen[n] = 0
        out.append(n)
    return out


# ---------------------------------------------------------------- 读取 XLSX

def read_xlsx(path, sheet=None):
    zf = zipfile.ZipFile(path)
    names = zf.namelist()

    # shared strings
    shared = []
    if "xl/sharedStrings.xml" in names:
        root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
        for si in root.findall(NS_MAIN + "si"):
            shared.append("".join(t.text or "" for t in si.iter(NS_MAIN + "t")))

    # styles -> 判断哪些 cellXf 是日期
    date_styles = set()
    if "xl/styles.xml" in names:
        try:
            root = ET.fromstring(zf.read("xl/styles.xml"))
            custom = {}
            numfmts = root.find(NS_MAIN + "numFmts")
            if numfmts is not None:
                for nf in numfmts.findall(NS_MAIN + "numFmt"):
                    fid = nf.get("numFmtId")
                    code = (nf.get("formatCode") or "").lower()
                    if fid:
                        custom[int(fid)] = code
            cellxfs = root.find(NS_MAIN + "cellXfs")
            if cellxfs is not None:
                for idx, xf in enumerate(cellxfs.findall(NS_MAIN + "xf")):
                    fid = int(xf.get("numFmtId") or 0)
                    if fid in _BUILTIN_DATE_FMT:
                        date_styles.add(idx)
                    elif fid in custom:
                        code = custom[fid]
                        if re.search(r"[dmyh]", code) and not re.search(r"^[#0.,%\s]*$", code):
                            date_styles.add(idx)
        except Exception as _dg_e:
            degrade.note("table.read_xlsx@L432", _dg_e)

    # workbook -> sheets
    wb = ET.fromstring(zf.read("xl/workbook.xml"))
    rels = {}
    if "xl/_rels/workbook.xml.rels" in names:
        rroot = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
        for rel in rroot.findall(NS_REL + "Relationship"):
            target = rel.get("Target", "")
            if target.startswith("/"):
                target = target[1:]
            elif not target.startswith("xl/"):
                target = "xl/" + target
            rels[rel.get("Id")] = target
    sheets = []
    for sh in wb.find(NS_MAIN + "sheets").findall(NS_MAIN + "sheet"):
        rid = sh.get(NS_DOCREL + "id")
        sheets.append((sh.get("name"), rels.get(rid, "xl/worksheets/sheet1.xml")))

    chosen = sheets
    if sheet:
        picked = [s for s in sheets if s[0] == sheet]
        if picked:
            chosen = picked
    tables = []
    for sname, spath in chosen:
        tables.append(_parse_sheet(zf, spath, shared, date_styles, sname, path))
    return tables[0] if len(tables) == 1 else tables


def _parse_sheet(zf, spath, shared, date_styles, sname, src):
    if spath not in zf.namelist():
        return Table([], [], sname, src)
    root = ET.fromstring(zf.read(spath))
    grid = {}
    max_c, max_r = -1, -1
    for c in root.iter(NS_MAIN + "c"):
        x, y = _cell_ref_to_xy(c.get("r"))
        t = c.get("t")
        s = c.get("s")
        if t == "inlineStr":
            is_el = c.find(NS_MAIN + "is")
            val = "".join(n.text or "" for n in is_el.iter(NS_MAIN + "t")) if is_el is not None else None
        else:
            v = c.find(NS_MAIN + "v")
            raw = v.text if v is not None else None
            if raw is None:
                val = None
            elif t == "s":
                idx = int(raw)
                val = shared[idx] if idx < len(shared) else None
            elif t == "b":
                val = "TRUE" if raw == "1" else "FALSE"
            elif t == "e":
                val = raw
            else:
                val = raw
                if s is not None and int(s) in date_styles:
                    dt = _excel_serial_to_datetime(raw)
                    if dt is not None:
                        val = dt.strftime("%Y-%m-%d %H:%M:%S")
        if val is not None and str(val).strip() != "":
            grid[(x, y)] = val
            max_c = max(max_c, x)
            max_r = max(max_r, y)
    if max_c < 0:
        return Table([], [], sname, src)

    # 找表头行：第一个非空单元格数 >= 2 的行
    header_row = 0
    for y in range(max_r + 1):
        cnt = sum(1 for x in range(max_c + 1) if (x, y) in grid)
        if cnt >= max(2, int((max_c + 1) * 0.5)):
            header_row = y
            break
    columns = _dedup([str(grid.get((x, header_row), "col_%d" % (x + 1))).strip()
                      for x in range(max_c + 1)])
    rows = []
    for y in range(header_row + 1, max_r + 1):
        row = [grid.get((x, y)) for x in range(max_c + 1)]
        if any(v is not None for v in row):
            rows.append(row)
    return Table(columns, rows, sname, src)


def read_any(path, sheet=None):
    ext = os.path.splitext(path)[1].lower()
    if ext in (".xlsx", ".xlsm"):
        return read_xlsx(path, sheet)
    if ext == ".xls":
        raise ValueError("旧版 .xls 不支持，请先另存为 .xlsx 或 .csv")
    if ext in (".txt", ".dat", ".log"):
        return read_csv(path)
    return read_csv(path)


# scan:keep 多工作簿读取。当前 XLSX 只取 --sheet 指定的一张表，需要全表合并时启用。
def read_multi(paths):
    """多文件读入 -> {文件名: Table}"""
    out = {}
    for p in paths:
        try:
            out[os.path.basename(p)] = read_any(p)
        except Exception as e:
            out[os.path.basename(p)] = {"error": str(e)}
    return out


# ---------------------------------------------------------------- 写出

def write_csv(table, path):
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d)
    with io.open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(table.columns)
        for r in table.rows:
            w.writerow(["" if v is None else v for v in r])


def _xml_escape(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def _col_letter(i):
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def write_xlsx(tables, path):
    """tables: Table 或 [Table, ...] -> 多 sheet xlsx"""
    if isinstance(tables, Table):
        tables = [tables]
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d)

    sheet_xmls = []
    for t in tables:
        buf = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
               '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>']
        # 表头
        buf.append('<row r="1">')
        for i, c in enumerate(t.columns):
            buf.append('<c r="%s1" t="inlineStr"><is><t xml:space="preserve">%s</t></is></c>'
                       % (_col_letter(i), _xml_escape(c)))
        buf.append('</row>')
        for ri, row in enumerate(t.rows, start=2):
            buf.append('<row r="%d">' % ri)
            for ci, v in enumerate(row):
                if v is None or v == "":
                    continue
                num = to_number(v)
                if num is not None and not isinstance(v, str) or (isinstance(v, str) and re.match(r"^-?\d+(\.\d+)?$", str(v).strip())):
                    buf.append('<c r="%s%d"><v>%s</v></c>' % (_col_letter(ci), ri, repr(float(num)) if num is not None else 0))
                else:
                    buf.append('<c r="%s%d" t="inlineStr"><is><t xml:space="preserve">%s</t></is></c>'
                               % (_col_letter(ci), ri, _xml_escape(v)))
            buf.append('</row>')
        buf.append('</sheetData></worksheet>')
        sheet_xmls.append("".join(buf))

    n = len(sheet_xmls)
    content_types = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
                     '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">',
                     '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
                     '<Default Extension="xml" ContentType="application/xml"/>',
                     '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>',
                     '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>']
    for i in range(n):
        content_types.append('<Override PartName="/xl/worksheets/sheet%d.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' % (i + 1))
    content_types.append('</Types>')

    root_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                 '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                 '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
                 '</Relationships>')

    wb = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
          '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
          'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>']
    for i, t in enumerate(tables, start=1):
        wb.append('<sheet name="%s" sheetId="%d" r:id="rId%d"/>' % (_xml_escape(t.name or ("Sheet%d" % i))[:31], i, i))
    wb.append('</sheets></workbook>')

    wb_rels = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
               '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">']
    for i in range(n):
        wb_rels.append('<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet%d.xml"/>' % (i + 1, i + 1))
    wb_rels.append('<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>' % (n + 1))
    wb_rels.append('</Relationships>')

    styles = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
              '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
              '<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>'
              '<fills count="1"><fill><patternFill patternType="none"/></fill></fills>'
              '<borders count="1"><border/></borders>'
              '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
              '<cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs>'
              '</styleSheet>')

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", "".join(content_types))
        z.writestr("_rels/.rels", root_rels)
        z.writestr("xl/workbook.xml", "".join(wb))
        z.writestr("xl/_rels/workbook.xml.rels", "".join(wb_rels))
        z.writestr("xl/styles.xml", styles)
        for i, sx in enumerate(sheet_xmls, start=1):
            z.writestr("xl/worksheets/sheet%d.xml" % i, sx)

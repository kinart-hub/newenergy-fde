# pandas 版参考实现（对照阅读用，非运行时代码）

`fde_pandas_reference.py` 是一份**参考实现**，提供同一套工艺数据分析流程的
「pandas 生态写法」，用于对照理解与二次开发。

**它不是 newenergy-fde 的运行时组成部分，也不会被打进分发包**
（`scripts/package.py` 的 `EXCLUDE_DIRS` 已排除 `pandas_reference`）。

---

## 为什么主线引擎不这么做

本项目铁律之一是**零第三方依赖**：CSV/XLSX 读写、统计、SPC、DOE、SVG 图表
全部用 Python 标准库实现。这不是不会用 pandas，而是交付约束决定的：

| 场景 | 依赖 pandas 的写法 | 主线（标准库）写法 |
| --- | --- | --- |
| 企业内网单机 | 需要 pip install，常因无外网/权限失败 | 拷贝即可运行 |
| 客户机器交付 | 需先配环境 | 无需任何安装 |
| 分发包体积 | 数百 MB（含 numpy） | 纯文本，几 MB |

参考实现保留下来，是为了让你能**对照两种写法的取舍**，也方便把它的方法论
嵌进你自己的 pandas 流水线。

---

## 怎么跑

```bash
pip install pandas numpy scipy openpyxl
python examples/pandas_reference/fde_pandas_reference.py
```

> 注意：脚本里的 `TMP_OUT_PATH = "/tmp/fde_analysis_result.xlsx"` 是类 Unix 路径，
> **在 Windows 上不存在**，会直接报错。Windows 下请改成例如：
> ```python
> TMP_OUT_PATH = os.path.join(os.path.expanduser("~"), "fde_analysis_result.xlsx")
> ```

---

## 与主线引擎的能力对照

| 能力 | 本参考实现 | 主线引擎 |
| --- | --- | --- |
| 读 CSV / XLSX | ✅ | ✅ |
| 空值剔除 | ✅ | ✅ |
| SPC 八判异 | ✅ | ✅ |
| Pareto | ✅ | ✅ |
| 输出 Excel 多 sheet | ✅ | ✅ |
| **工序自动判定** | ❌ | ✅ |
| **设备档案层（竖表/多级表头/单位行归一）** | ❌ | ✅ |
| **跨工序追溯（本工序主导 / 上游带入）** | ❌ | ✅ |
| **过程能力 Cpk（含规格来源声明）** | ❌ | ✅ |
| **相关性 + 分层 ANOVA** | ❌ | ✅ |
| **5Why / 根因证据链** | ❌ | ✅ |
| **DOE 正交表 + 安全包络** | ❌ | ✅ |
| **工艺分析报告 + 8D 初稿（D1–D8）** | ❌ | ✅ |
| **D5–D8 确审闸门** | ❌ | ✅ |
| **知识库演进 / 租户隔离 / 行业作用域** | ❌ | ✅ |
| **自检 279 项 + 护栏 86 项** | ❌ | ✅ |

---

## 已知局限（使用这份代码前请先看）

1. **控制限用全段数据计算**：`sigma = data.std(ddof=1)` 包含异常点，
   异常会把 σ 撑大、控制限变宽，最该报警的点反而落在限内。
   主线引擎对此已做 **SPC Phase I/II 分离**（稳定期建限，再判后续）。
2. **参数阈值写死**：`涂布厚度 80~120`、`硅片厚度 140~180` 硬编码在 `np.where` 里，
   换产品/换行业就要改代码。主线要求规格由调用方通过 `--spec` 传入。
3. **`raise Warning(...)`** 是 Python 反模式（`Warning` 是类、不是异常），
   正确写法是 `warnings.warn(...)`；否则这段"兜底"本身也会抛 `TypeError`。
4. **`from scipy import stats` 未被使用**，可删。
5. **只输出 Excel**，没有工艺分析报告、8D 初稿与图表。

> 需要能直接发给客户的完整交付物，请用主线入口：
> `python scripts/fde_run.py analyze -i <数据> -o <输出>`

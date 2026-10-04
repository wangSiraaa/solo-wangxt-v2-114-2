# 固定样地复测森林生长 / 死亡 / 进界量评估系统

林业研究站比较固定样地多次调查（本演示为 **2019 → 2024 → 2029** 三期）的：

* **存活木生长量**（survivor growth）
* **死亡量**（mortality）
* **进界量**（ingrowth，胸径 ≥ 5 cm 阈值）

净变化恒等式（对每个**相邻区间**分别成立）：

```
Δ生物量 = 存活木生长 − 死亡 + 进界
```

技术栈：**React（Vite）+ Django REST Framework + NumPy/SciPy + PostgreSQL/PostGIS**
（开发环境用 sqlite3 也能完整运行；PostGIS 层见 `deploy/postgis.sql`）。

> 数据均为**虚构**演示数据（树种、方程、坐标、样地），仅用于说明流程。

---

## 0. 多期调查序列（2029 第三次复测）

系统不是"只比较两期的报表"：调查序列（`SurveySequence`）把各期调查按时间排成
**相邻区间链** 2019→2024、2024→2029，每个区间（`SurveyInterval`）独立保存：

* **区间覆盖状态** `coverage`：`pending`（远端未测）/ `partial`（部分样地有数）/
  `covered`（全部样地两端有数）；
* **身份判定**（`IntervalIdentityLink`）：每株个体在该区间内的角色——存活、死亡、
  进界、未找到、未决项（见下）；
* **估计版本**（`EstimateVersion.interval`）：该区间自己的 draft/confirmed 版本。

关键规则：

1. **身份关系只在相邻区间内成立。** 2019 与 2029 之间不存在直接配对；链上净变化是
   相邻区间净变化之和，而不是首尾拼接。API 层拒绝为非相邻区间（如 2019→2029）
   创建估计（400）。
2. **跨期缺测、换号、位置矛盾必须留下待核实链路（pending link）**：
   * 2024 为 `missing_tree`、2029 又出现的个体 → `gap_reappearance`（pending），
     **不得自动跨缺口计为存活增长**，核实前排除在所有分量之外；
   * 2029 的近邻换号（新牌号出现在旧位置附近）→ 仅生成 **2024→2029 区间**的
     `possible_renumber` 待核实项，2019→2024 区间不受影响；
   * 同号位置矛盾 → 该区间的 `IdentityConflict`（open），剔除待核实。
3. **既有两期结果与已确认版本不得被回写。** 旧的 confirmed 版本（sequence 建立前
   创建的，`interval=NULL`）按 t1/t2 区间**只读匹配**进区间视图，行本身永不修改；
   sync/recompute 只会为没有版本的区间**新增 draft**。
4. **幂等。** 区间按 (sequence, t1, t2) 唯一；身份链路按 (interval, tree) 唯一；
   冲突按 (t1_measurement, t2_measurement) 唯一；入库按 (tree, campaign)
   update_or_create。同一批 2029 数据重传或失败重试不会重复建立区间和来源。

---

## 1. 关键规则（对应验收要求）

### 1.1 胸径单位必须显式记录
* 每条测量必须带 `dbh_unit`（`cm`/`mm`/`in`），树高单位 `m`。
* 入库时转换为规范单位（胸径 cm、树高 m），**原始值与单位同时保留**，可审计。
* 合理范围检查拦截单位错误（如把 250 mm 当成 250 cm、树高 950 m）。
* 树干坐标必须落在样地边界内（PostGIS 层有 `ST_Contains` 约束兜底）。

### 1.2 异速生长方程显式记录适用树种
`AGB_kg = a · dbh_cm^b · h_m^c`，方程记录：
* 适用树种列表（多对多）、胸径适用范围、是否需要树高；
* 系数 a/b/c、残差 σ(ln AGB)、文献引用、版本号；
* 超出适用径阶的树会在结果中标记 **extrapolation**。

### 1.3 编号是标签，不是身份
**编号相同但位置矛盾时，先核实，不能直接认成同株。**

* 内部 `tree_id` 才是个体身份；同树行换标签 = 已核实改号（renumber）。
* 同编号、不同树行、位置矛盾 → 生成 `IdentityConflict`（open），
  在人工核实前**从所有分量中剔除**，不会悄悄变成死亡或进界。
* 核实结论只有人工给出：
  * `renumber`：同一株树换了牌号 → 计入存活木生长；
  * `distinct`：不同个体 → t1 计入死亡、t2 计入进界。
* 新编号出现在旧树附近 → “possible renumber” 待核实，绝不自动合并。

### 1.4 真实零生长、缺测、死亡三者严格区分
| 情况 | 字段 | 处理 |
|---|---|---|
| 真实零生长 | `alive_measured`，两次均测，|Δdbh| ≤ 0.15 cm 且有复核记录 | 计入生长量（增量≈0），结果中列出 |
| 缺测 | `alive_not_measured`（活着但胸径未测） | **不是零**；按样地比率插补，方差膨胀，列出清单 |
| 死亡 | `dead`（t2 有死亡观测） | 以 t1 生物量计入死亡量 |
| 未找到 | `missing_tree` | 不入死亡量，列入 provenance |
| 进界以下 | t2 新树 dbh < 5 cm | 记录但不计入进界 |

### 1.5 总体估计按抽样设计加权
分层简单随机抽样，**不把所有树木平均后乘面积**：

```
样地分量 y [kg/ha] = 分量(kg) / 该样地自己的面积(ha)
Y_h = A_h · mean_h(y)                 # A_h = 已知的层土地面积
SE_h = A_h · sqrt( (1−f_h) · s_h²/n )  # 有限总体校正可选
Y = Σ_h Y_h，SE 跨层合成（Welch–Satterthwaite 自由度，t 分布 95% CI）
```

演示数据刻意使用**不等面积样地**（0.20 / 0.50 / 1.00 ha）。

### 1.6 已确认调查版不可被新方程静默改变
* `EstimateVersion`：draft → `confirm` 后结果载荷、设计快照、方程校验和全部冻结。
* 模型层 + PostGIS 触发器双重禁止修改 confirmed 版本。
* 确认时同时**锁定所用方程**（系数不可改）；新系数必须以**新方程 code/version** 录入，
  并产生**新版本估计**，旧版本数字永不改变。

---

## 2. 不确定性假设（结果中完整输出）
1. **设计推断**：分层 SRS，每样地等权（每公顷基准），层土地面积放大；树木从不合并平均。
2. **测量误差**：胸径 σ=0.10 cm、树高 σ=0.30 m，独立高斯，一阶误差传播；
   作为诊断分量单独报告（不与样地间抽样方差重复计入 SE 合计）。
3. **方程残差**：乘法对数正态 σ(ln AGB)；存活木两次用同一方程、残差假定完全相关故增量抵消；
   死亡（仅 t1）与进界（仅 t2）的方程误差保留。
4. **缺测存活木**：假定样地内 MAR，用“有测样木 t1 生物量生长率”做比率插补，
   抽样方差按 1/(1−缺测生物量比例) 膨胀。
5. 未核实身份、未找到、进界以下个体**排除在分量外**并在 provenance 列明。
6. 95% CI 用跨层 Welch–Satterthwaite df 的 t 分布；净变化 SE 中分量抽样协方差假定为 0。
7. 样地申报面积与多边形面积交叉核对（1% 容差）。

---

## 3. 运行

### 后端
```bash
cd backend
python3 -m venv .venv && . .venv/bin/activate      # 可选
pip install -r requirements.txt
python3 manage.py migrate
python3 manage.py seed_demo        # 虚构数据：2019/2024 两期 + 序列 + confirmed 基线
python3 manage.py seed_2029        # 第三次复测：补入 2029，链上新增 2024→2029 区间
python3 manage.py runserver 127.0.0.1:8123
```

PostgreSQL/PostGIS：
```bash
FOREST_DB=postgis PGHOST=.. PGUSER=.. PGPASSWORD=.. \
  python3 manage.py migrate
psql -d foreststation -f ../deploy/postgis.sql
```

### 前端
```bash
cd frontend
npm install
npm run dev          # http://localhost:5173, /api 代理到 8123
```

界面四页：
1. **Plots & individuals**：SVG 地图显示全部样地边界与所选相邻区间 t2 个体状态；
   区间选择器切换 2019→2024 / 2024→2029；点入样地看该区间复测、改号、
   零生长/缺测/死亡着色；
2. **Timeline (interval chain)**：区间链总览（每期节点 + 区间卡片：覆盖状态、
   G/M/I、待核实数、版本）；点开区间看来源与待核实链路；按样地展开个体×各期
   矩阵，再点个体看完整时间线；
3. **Identity conflicts**：编号矛盾核实工作台（renumber / distinct），按区间作用；
4. **Estimates**：选择区间 + 方程→跑 draft→查看分量、来源、不确定性→确认冻结。

---

## 4. API 摘要
| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/plots/` | 样地位置、边界、面积、CRS |
| GET | `/api/measurements/?campaign=2024` | 每株每期测量（含原始/规范单位） |
| GET | `/api/equations/` | 方程、系数、适用树种、径阶范围 |
| GET | `/api/conflicts/?status=open` | 同号位置矛盾（可按 `?interval=` 过滤） |
| POST | `/api/conflicts/{id}/resolve/` | `{status: renumber|distinct, note}` |
| POST | `/api/imports/` | 批量入库（拒收单位错误/越界行，207 返回明细）；仅与相邻期扫描冲突 |
| POST | `/api/estimates/` | 运行 draft 估计（`interval_id` 或相邻 t1/t2；非相邻 400） |
| POST | `/api/estimates/{id}/confirm/` | 冻结版本并锁定方程 |
| GET | `/api/estimates/{id}/` | 完整结果：分量 + 来源 + 不确定性 |
| GET/POST | `/api/sequences/` | 调查序列列表 / 创建（按 name 幂等） |
| POST | `/api/sequences/{id}/sync/` | 补齐序列：纳入新期、建相邻区间、刷新覆盖与身份链路，可选为新区间建 draft |
| GET | `/api/sequences/{id}/plots/{code}/timeline/` | 样地内个体×各期×各区间矩阵 |
| GET | `/api/trees/{id}/timeline/` | 单株全序列时间线 |
| GET | `/api/intervals/{id}/provenance/` | 区间来源：入库批次、身份判定、未决冲突、估计版本 |
| POST | `/api/intervals/{id}/recompute/` | 重算单个区间（可选 `run_estimate` 新增 draft；confirmed 不受影响） |

### 入库行示例
```json
{
  "campaign": "2024",
  "rows": [{
    "plot": "P01", "field_number": "001", "species": "OAK",
    "x_m": 500010.0, "y_m": 4000010.0,
    "status": "alive_measured",
    "dbh_raw": 252, "dbh_unit": "mm",
    "height_raw": 16.8, "height_unit": "m"
  }]
}
```

---

## 5. 验收测试
```bash
cd backend && python3 manage.py test inventory
```
21 个测试覆盖：改号、同号位置矛盾（剔除→核实 distinct 后才入死亡/进界）、
不等面积按样地扩展、单位错误拒收、零生长/缺测/死亡区分、已确认版本对新方程与直接篡改免疫，
以及多期序列验收：

* 正常补入 2029 后**只新增 2024→2029 一段**的估计，2019→2024 区间仍只有原 confirmed 版；
* 2024 为 `missing_tree`、2029 又出现的个体生成 `gap_reappearance` 待核实链路，
  **不跨缺口计为存活增长**（也不入死亡/进界）；
* 2029 的近邻换号只生成 **2024→2029 区间**的待核实身份项，2019→2024 区间无变化；
* 同一批 2029 数据重传 / sync 重试：区间、身份链路、冲突、版本、树、测量数全部不变；
* 直接查看原 2019→2024 confirmed 版本：载荷、设计快照、校验和逐字节不变；
* 直接对 2019→2029 建估计被 400 拒绝（禁止跨缺口拼接）。

## 6. 虚构演示数据场景索引
* `P01/004` 两次胸径相同 → **真实零生长**；
* `P01/005` 活着未测胸径 → **缺测比率插补**；`P02/003`、`P04/006` 同；
* `P01/006`、`P03/004`、`P04/005` → **死亡**；`P02/005`、`P05/006` → **未找到**；
* `P01/007→017` → **已核实改号**（同一 tree 行）；
* `P01/008`、`P01/009` → **同号位置矛盾，open 剔除**；`P02/117→118` 疑似改号 open；
* `201` 系列（dbh 4.2–6.4）→ 进界阈值边界，<5 cm 排除；
* `P04/002` dbh 102 cm → **超出方程径阶范围**标记；
* 4 条坏行（mm 当 cm、树高 cm 当 m、缺单位、坐标越界）→ **入库拒收**；
* 样地面积 0.20 / 0.50 / 1.00 ha 不等。

### 2029 第三次复测（`seed_2029`）
* `P02/005` 2024 `missing_tree`、2029 原位复测 → **gap_reappearance 待核实**，不计存活增长；
* `P02/006 → 106` 0.45 m 近邻换号 → 仅 2024→2029 区间的 possible_renumber 待核实；
* `P05/004` 同号 18.7 m 位置矛盾 → 该区间 open 冲突，原树与新行双双剔除；
* `P05/006` 2024 缺测、2029 发现死亡 → 死亡时间不可定，列出但不计入死亡量；
* `P05/002` 2029 活着未测 → 缺测插补；`P01/005`、`P02/003`、`P04/006` 2024 缺测 2029 复测；
* `P01/203`、`P01/204` → 2024→2029 进界；`P01/201` 区间内跨过 5 cm 进界阈值；
* `P01/008`、`P01/009` 的 2019→2024 冲突仍在原区间 open；2024→2029 区间内
  这些树行是已确立个体（身份关系不跨区间传播）。

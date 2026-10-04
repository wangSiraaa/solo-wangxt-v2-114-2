# 固定样地复测森林生长 / 死亡 / 进界量评估系统

林业研究站比较固定样地**按相邻时期组成的调查序列**（本演示为
**2019 → 2024 → 2029** 三个时期、两个区间）的：

* **存活木生长量**（survivor growth）
* **死亡量**（mortality）
* **进界量**（ingrowth，胸径 ≥ 5 cm 阈值）

净变化恒等式（在**每个相邻区间内**分别成立）：

```
Δ生物量 = 存活木生长 − 死亡 + 进界
```

身份关系**只在对应相邻区间内成立**：系统按相邻时期建立可追溯的区间链，
分别保存区间覆盖状态（coverage snapshot）、逐株身份判定（IntervalLink）和
估计版本（EstimateVersion）。2019 与 2029 **永远不会直接配对**——树木跨期
缺测、换号或位置矛盾时留下待核实链路（pending chain item），而不是悄悄
拼成连续存活。

技术栈：**React（Vite）+ Django REST Framework + NumPy/SciPy + PostgreSQL/PostGIS**
（开发环境用 sqlite3 也能完整运行；PostGIS 层见 `deploy/postgis.sql`）。

> 数据均为**虚构**演示数据（树种、方程、坐标、样地），仅用于说明流程。

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

### 1.7 多期序列：相邻区间链，绝不跨缺口拼接
三期复测（2029 第三次复测）不再走两期报表逻辑：

* `SurveySequence`（序列）→ `SequenceMembership`（有序期序）→
  `SurveyInterval`（**相邻**区间，如 2019→2024、2024→2029）。
* 每个区间独立保存三类制品，互不回写：
  * **覆盖快照** `coverage_snapshot`：每端状态计数、逐样地覆盖、数据指纹
    （SHA-256），重算指纹不变即可追溯；
  * **逐株身份判定** `IntervalLink`：该树在**本区间**的结局
    （same-number 存活、verified 改号、零生长、未测、死亡、进界、未找到、
    待核实项），判定来源 `data` / `human` / `pending`；
  * **估计版本** `EstimateVersion`：区间严格估计的 draft / confirmed，
    confirmed 永不改。旧的两期 confirmed 版本（`interval_id = null`）保持原样。
* **跨缺口规则（strict gap chain）**：
  * 同一 tree 行在区间 t1 为 `missing_tree`/`dead`、t2 又存活，
    或该 tree 在本区间 t1 **完全无记录**但更早调查期出现过（2019 有、
    2024 缺、2029 重现）→ 生成 `gap_reappearance` **待核实项**；
  * 该株从本区间全部分量中剔除：**不**计存活增长、**不**计进界、**不**计死亡；
  * 人工核实后仍保持剔除（核实不能凭空补出缺测那一期的数据）。
* **换号只属于所在区间**：2029 的近邻换号只在 2024→2029 生成
  `possible_renumber` 待核实项，不会在 2019→2024 产生任何记录。
* **入库幂等**：`ImportBatch`（`client_batch_id` 或载荷指纹）保证同一批
  2029 数据重传、或失败重试只回放已存结果，不重复建树、审计行、冲突或区间链路。
* 补入 2029 **只新增后一段**：`add_campaigns` 只创建缺失的相邻区间；
  已 built 区间与其 confirmed 版本不被重算覆盖（重算只刷新 draft 用的链路，
  从不改冻结载荷）。

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
python3 manage.py seed_demo        # 载入虚构数据（含全部验收场景）
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
1. **Plots & individuals**：SVG 地图显示全部样地边界与 t2 个体状态；点入样地看 t1→t2 复测、
   改号、零生长/缺测/死亡着色；
2. **Multi-campaign timeline**：按样地与个体展开 2019｜2019→2024｜2024｜2024→2029｜2029
   时间线；每个箭头是独立区间链路，跨缺口/换号/矛盾显示 ⛓⚠ VERIFY 断边，
   绝不画成 2019→2029 的连续存活；
3. **Identity items**：编号矛盾 / 近邻换号 / 跨缺口重现核实工作台
   （显示所属区间；renumber / distinct；gap 核实后仍剔除）；
4. **Estimates**：选择区间跑 **strict chain** draft（只在该相邻区间成立），
   或跑旧的两期 legacy draft（非严格）；查看分量、来源、不确定性→确认冻结。

---

## 4. API 摘要
| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/plots/` | 样地位置、边界、面积、CRS |
| GET | `/api/measurements/?campaign=2029` | 每株每期测量（含原始/规范单位） |
| GET | `/api/equations/` | 方程、系数、适用树种、径阶范围 |
| GET | `/api/conflicts/?status=open&interval=ID` | 逐区间身份项（含 hint） |
| POST | `/api/conflicts/{id}/resolve/` | `{status: renumber\|distinct, note}`，并重建所在区间链路 |
| POST | `/api/imports/` | 批量入库；`client_batch_id` 或相同载荷指纹保证幂等（重传/重试回放） |
| GET | `/api/import-batches/?campaign=2029` | 入库批次（幂等审计信封） |
| POST | `/api/estimates/` | **legacy 两期** draft（非严格，区间为空） |
| POST | `/api/estimates/{id}/confirm/` | 冻结版本并锁定方程 |
| GET | `/api/estimates/{id}/` | 完整结果：分量 + 来源 + 不确定性 |
| POST | `/api/sequences/` | 创建调查序列（相邻区间自动建链），`{code,name,campaigns:[...]}` |
| GET | `/api/sequences/` | 序列、期序、区间数 |
| POST | `/api/sequences/{id}/add_campaigns/` | **补齐序列**（如补入 2029）：只新增缺失的相邻区间 |
| GET | `/api/intervals/?sequence=MAIN` | 全部区间（覆盖状态、链路数、待核实数、版本） |
| GET | `/api/intervals/{id}/` | 单个区间（含 coverage 快照与指纹） |
| POST | `/api/intervals/{id}/refresh/` | **重算区间**：重扫身份项、重建覆盖与链路（幂等；不动冻结版本） |
| GET | `/api/intervals/{id}/provenance/` | **区间来源**：两端测量、身份项、逐株链路、版本清单 |
| POST | `/api/intervals/{id}/estimates/` | 该区间的 **strict gap-chain** draft |
| GET | `/api/timeline/?sequence=MAIN&plot=P01` | 按样地展开的多期时间线 |
| GET | `/api/timeline/?sequence=MAIN&tree=ID` | 按个体展开的时间线（断边=待核实） |

### 入库行示例
```json
{
  "campaign": "2029",
  "client_batch_id": "field-2029-P01-003",
  "rows": [{
    "plot": "P01", "field_number": "001", "species": "OAK",
    "x_m": 500010.0, "y_m": 4000010.0,
    "status": "alive_measured",
    "dbh_raw": 263, "dbh_unit": "mm",
    "height_raw": 17.5, "height_unit": "m"
  }]
}
```

`client_batch_id` 可选：同一 campaign + 同一批次号（或字节一致的载荷）重复 POST
会返回 `idempotent_replay: true` 与原批次结果，不会二次入库；同批次号但行
内容不同返回 409。

### 多期建链示例
```bash
# 1) 创建前两期（区间自动 built）
curl -X POST .../api/sequences/ -d '{"code":"MAIN","name":"主复测",
  "campaigns":["2019","2024"]}'
# 2) 正常补入 2029：只新增 2024->2029 一段
curl -X POST .../api/imports/ -d '{"campaign":"2029","client_batch_id":"b29",
  "rows":[...]}'
curl -X POST .../api/sequences/1/add_campaigns/ -d '{"campaigns":["2029"]}'
# 3) 查询区间来源 / 重算区间 / 跑严格估计
curl .../api/intervals/2/provenance/
curl -X POST .../api/intervals/2/refresh/
curl -X POST .../api/intervals/2/estimates/ -d '{"equation_ids":[1,2,3]}'
```

---

## 5. 验收测试
```bash
cd backend && python3 manage.py test inventory
```
共 **20 个测试**：

* 原有 12 个：改号、同号位置矛盾（剔除→核实 distinct 后才入死亡/进界）、
  不等面积按样地扩展、单位错误拒收、零生长/缺测/死亡区分、已确认版本对新方程与
  直接篡改免疫；
* 多期 8 个（`tests_sequence.py`）：
  1. 正常补入 2029 后**只新增后一段**区间与估计，2019→2024 confirmed 原样返回；
  2. **2024 `missing_tree`、2029 重现**（同行缺口 + 无 2024 行但 2019 已知）
     只生成 2024→2029 的 `gap_reappearance` 待核实项，严格估计生长/死亡/进界全为 0，
     时间线为断边；人工核实后仍不计增长；
  3. **2029 近邻换号**只生成 2024→2029 的 `possible_renumber`，2019 区间无记录；
  4. 同一批 2029 **重传 / 无批次号同载荷重试**全部回放（`idempotent_replay`），
     测量行/审计行/批次/冲突/区间链路数量不增加；同批次号不同内容返回 409；
  5. **legacy confirmed 2019→2024** 版本（`interval_id=null`、非严格标记）
     仍返回原冻结数字与快照；
  6. 序列创建 / `add_campaigns` 补齐 / provenance / refresh 幂等 / 个体时间线 API。

## 6. 虚构演示数据场景索引
### 2019 → 2024 区间
* `P01/004` 两次胸径相同 → **真实零生长**；
* `P01/005` 活着未测胸径 → **缺测比率插补**；`P02/003`、`P04/006` 同；
* `P01/006`、`P03/004`、`P04/005` → **死亡**；`P02/005`、`P05/006` → **未找到**；
* `P01/007→017` → **已核实改号**（同一 tree 行）；
* `P01/008`、`P01/009` → **同号位置矛盾，open 剔除**；`P02/117→118` 疑似改号 open；
* `201` 系列（dbh 4.2–6.4）→ 进界阈值边界，<5 cm 排除；
* `P04/002` dbh 102 cm → **超出方程径阶范围**标记；
* 4 条坏行（mm 当 cm、树高 cm 当 m、缺单位、坐标越界）→ **入库拒收**；
* 样地面积 0.20 / 0.50 / 1.00 ha 不等。

### 2024 → 2029 区间（第三期复测新增）
* `P01/005`、`P04/006`：2024 未测（`alive_not_measured`）、2029 找到并测量
  → **跨缺口重现待核实**（不计 2024→2029 存活增长）；
* `P02/005`：2024 `missing_tree`、2029 原位重现
  → **跨缺口重现待核实**（gap_reappearance）；
* `P05/006`：2024、2029 连续未找到 → 不伪造连续存活；
* `P01/301`：2029 新号、距存活木 `P01/002` 1.2 m
  → **近邻换号待核实**（仅本区间，`002` 仍按原号正常生长）；
* `P02/202`、`P03/201` 等：**2029 进界**（≥ 5 cm）；
* 其余为正常存活木，其生长量只计入 2024→2029 一段，2019→2024 数字不变。

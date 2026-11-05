# 香港置业二手住宅成交采集器

这是一个 Python 命令行程序。它从香港置业的公开成交列表页开始，以浏览器可见分页方式收集资料，筛选 2023-01-01 至程序运行当天的二手住宅买卖成交，并生成 Ridge/Lasso 可直接使用的 CSV 与 Excel 数据集。

程序不会登录、不会处理验证码、不会绕过访问限制，也不会调用未公开的香港置业数据接口。它会检查公开页面的 `robots.txt`，以单线程、限速方式读取页面；遇到验证码、403、429 或登录页会停止。

## 数据范围

每条最终记录必须同时符合以下条件：

- 香港时间介于 `2023-01-01` 与程序运行当天。
- 买卖成交：`tx_type = S`。
- 二手／二级市场：`mkt_type = 2`。
- 住宅单位；纯车位、商铺、写字楼及工业物业会排除。
- 每个输入 URL 对应一个屋苑或明确期数，而不是一宗单独成交。

## 安装

需要 Python 3.10 或以上。请在本项目目录打开 PowerShell：

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e .
python -m playwright install chromium
```

最后一行会安装用于公开网页分页的 Chromium 浏览器。它只在程序运行时以无头模式读取正常浏览器可见的公开页面。

若 PowerShell 提示禁止运行 `Activate.ps1`，只需在当前窗口临时执行：

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

该设置在关闭 PowerShell 后自动失效，不会修改系统全局执行策略。

## Google Maps 配置

请在同一个 Google Cloud 项目启用：

- Routes API
- Places API (New)

使用一个 API Key，并在 PowerShell 当前会话设置环境变量：

```powershell
$env:GOOGLE_MAPS_API_KEY = "YOUR_GOOGLE_ROUTES_API_KEY"
```

虽然变量示例沿用 `YOUR_GOOGLE_ROUTES_API_KEY` 的占位名称，该 Key 必须同时具有 Routes API 与 Places API (New) 权限。程序不会把 Key 写入代码、CSV、XLSX、SQLite 或日志。

## 准备 `tasks.csv`

输入文件只需要一列，列名必须为 `hkp_list_url`。每一行是一个屋苑或期数的香港置业**成交列表页** URL。

```csv
hkp_list_url
https://www.hkp.com.hk/zh-cn/list/transaction/天晋-E-E000010411
https://www.hkp.com.hk/zh-cn/list/transaction/某屋苑某期-P-P000000000
```

一个列表页 URL 可发现多宗成交；不需要逐宗复制成交详情页 URL。请从浏览器复制完整公开列表页 URL，包含中文或百分号编码均可。

## 运行

```powershell
hkp-transactions crawl --tasks tasks.csv --database data/transactions.sqlite --csv outputs/transactions.csv --xlsx outputs/transactions.xlsx --source-cache data/sources --max-records 500
```

等效的模块调用方式：

```powershell
python -m hkp_transaction_crawler crawl --tasks tasks.csv --database data/transactions.sqlite --csv outputs/transactions.csv --xlsx outputs/transactions.xlsx --source-cache data/sources --max-records 500
```

参数说明：

- `--tasks`：仅含 `hkp_list_url` 一列的任务 CSV。
- `--database`：SQLite 断点续跑、去重与内部审计数据库。
- `--csv`：适合 Python、R、Stata 或 Ridge/Lasso 的 UTF-8-BOM CSV 输出。
- `--xlsx`：用于人工核对的 Excel 输出。
- `--source-cache`：官方学校、交通、港铁及指数资料的本地快照缓存目录。
- `--max-records`：每次运行最多记录的数据条数，默认 `500`。
- `--google-max-requests`：每次运行最多发出的 Google Places／Routes 安全请求上限，默认 `100000`。
- `--timeout`：单次公开网页或浏览器等待秒数，默认 `60`；网络较慢时可改为 `90`。

达到单次记录上限（500 条）或异常中断时，程序会自动保存已完成的数据至 SQLite 并导出 CSV/XLSX。以相同参数再次运行即可从中断的数据接着录，不会重复请求已保存成交的详情页或已缓存的楼宇地理 factor。

## 预计耗时

香港置业网页以保守限速读取。100 宗成交若集中在少量屋苑／楼宇，通常约需 8–15 分钟；若分布在许多不同楼宇，通常约需 15–30 分钟。Google API 网络状况、缓存命中率、网页临时限制与重试都会影响实际时间。

## 最终输出结构

CSV 与 XLSX 都严格使用 A:AB，共 28 列。XLSX 第 1 行为机器可读字段名，第 2 行为中文说明，数据从第 3 行开始；所有派生列均写入已经计算好的数值，不使用 Excel 公式。所有导出的浮点数均实际四舍五入至最多 4 位小数。

| 列 | 字段 | 含义 |
| --- | --- | --- |
| A | `record_id` | 香港置业公开成交记录编号 |
| B | `source_url` | 原始公开成交详情页链接 |
| C | `transaction_date` | 香港时间成交日期 |
| D | `price_hkd` | 成交总价（HKD） |
| E | `log_price` | `LN(price_hkd)`，回归目标变量 |
| F | `transaction_month_index` | 成交月份序号，`2020-01 = 1` |
| G | `district` | 香港置业的较大分区，例如“将军澳” |
| H | `saleable_area_sqft` | 实用面积（呎） |
| I | `log_area` | `LN(saleable_area_sqft)` |
| J | `bedrooms` | 开放式为 0，5 房以上为 5 |
| K | `completion_year` | 入伙年份 |
| L | `property_age` | `YEAR(transaction_date) - completion_year` |
| M | `property_age_sq` | `property_age` 的平方 |
| N | `floor` | 具体楼层（如 `16`、`G`），未提供数据填 `NA` |
| O | `floor_band` | `low`、`mid`、`high` 或 `unknown` |
| P | `property_type` | `tower`、`house`、`other` 或 `unknown` |
| Q | `has_balcony` | 连阳台，0/1 |
| R | `has_living_balcony` | 连生活阳台，0/1 |
| S | `has_terrace` | 连露台（平台），0/1 |
| T | `has_parking` | 连车位，0/1 |
| U | `has_rooftop` | 连天台，0/1 |
| V | `has_garden` | 连花园，0/1 |
| W | `mtr_walk_min` | 至最近港铁／轻铁站的步行分钟 |
| X | `mall_walk_min` | 至最近正式购物商场的步行分钟 |
| Y | `school_count_within_1km` | 1 km 内正式学校数量 |
| Z | `nearest_hk8_university_distance_km` | 至最近香港八大主校园的步行路线距离（km） |
| AA | `medical_walk_min` | 至最近医院、政府诊所或综合医疗中心的步行分钟 |
| AB | `district_price_index` | 成交月份对应的官方区域月度楼价指数 |

对于回归，`district`、`floor_band` 与 `property_type` 应在建模阶段做 one-hot 编码。`price_hkd` 是原始价格，`log_price` 是建议的目标变量。不要把 `saleable_area_sqft` 与 `log_area` 同时作为同一个线性模型的面积表达式，除非你有明确的正则化与解释策略。

## 地理 factor 的口径与资料来源

所有地理设施均采用**程序运行时的当前资料快照**，而不是每宗成交日的历史设施状态。程序会保存快照日期于内部数据库；若无坐标、资料或路线结果，相关 factor 留空，不用 0 或 999 代替。

| Factor | 口径与来源 |
| --- | --- |
| `mtr_walk_min` | 地政总署 iGeoCom 的铁路站出入口为优先候选；Google Routes 计算步行时间。含重铁、机场快线和轻铁；官方铁路出入口资料不可用时才回退至 Google Places。 |
| `mall_walk_min` | Google Places 找正式购物商场／购物中心，再以 Google Routes 计算步行时间；不含街市、超级市场和单独百货店。 |
| `school_count_within_1km` | 教育局正式学校名册；包括幼稚园、中小学、特殊学校及国际学校，不含大学／其他专上院校、补习社、兴趣班、语言中心或一般教育中心。同名、同址、同教育阶段的上午／下午／全日制资料合并为一所。 |
| `nearest_hk8_university_distance_km` | 香港八大主校园坐标为终点，Google Routes 计算步行路线距离。 |
| `medical_walk_min` | Google Places 找医院、政府诊所及综合医疗中心，再以 Google Routes 计算步行时间；不含药房、牙科及个别医生诊所。 |
| `district_price_index` | 香港置业市场洞察的区域月度指数：香港岛映射 `mr_index_hk`、九龙映射 `mr_index_kln`、新界映射 `mr_index_nt`。最新月份尚无正式月度值时，使用之前最近一个正式月度值。 |

主要官方来源：

- [香港置业市场洞察](https://www.hkp.com.hk/zh-cn/market-insight)
- [地政总署 iGeoCom 社区设施资料](https://data.gov.hk/en-data/dataset/hk-landsd-openmap-development-hkms-digital-geocom/resource/2d7f57de-1b8e-4aa0-b6d8-c8a324fe22cb)
- [香港运输署公共交通路线及站点资料](https://data.gov.hk/en-data/dataset/hk-td-tis_3-routes-and-fares-of-public-transport)
- [香港运输署公共交通 GTFS 站点资料](https://data.gov.hk/en-data/dataset/hk-td-tis_11-pt-headway-en)
- [港铁轻铁路线及站点资料](https://data.gov.hk/en-data/dataset/mtr-data-routes-fares-barrier-free-facilities/resource/d0662909-9919-4f01-b4f9-eccc4b2acbb5)
- [教育局学校位置资料](https://data.gov.hk/en-data/dataset/hk-edb-schinfo-school-location-and-information/resource/fbd6d757-e3b1-4a67-a98e-8560f38f0468)
- [教资会资助院校地理资料](https://data.gov.hk/en-data/dataset/hk-ugc-ugc-institution-location/resource/317f3573-e4b8-4708-8ebf-601dde67cae6)

## 重要限制

- 公开网页、字段、筛选条件或 `robots.txt` 改变时，程序可能需要更新；不要绕过网站限制。
- Google API 的配额、计费和条款由你的 Google Cloud 项目控制。请同时设置 Google Cloud 预算提醒与 API 配额；本程序的 1,000 次请求上限是额外保护，不等同于货币预算。
- 当前地理快照可能将成交后新增的学校、交通或设施带入较早的 2023 年交易。该设计已按项目要求采用当前资料；如需严格成交日历史设施状态，需要另建历史地理资料流程。
- 住宅 feature 的 0/1 值按香港置业 feature ID 计算。页面未列出某 feature 时为 0；坐标、路线与外部资料查询失败时留空。

## 测试

安装依赖后可运行离线测试：

```powershell
python -m unittest discover -s tests -v
```

离线测试不请求香港置业、Google Maps 或开放数据站点。

## 许可

MIT。使用任何来源数据前，请遵守香港置业、Google Maps Platform、DATA.GOV.HK 及相关资料提供方的条款和适用法律。


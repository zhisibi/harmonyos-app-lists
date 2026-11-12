# harmonyos-app-lists

一个兴趣项目：自动从华为应用市场公开接口采集、核实鸿蒙（HarmonyOS NEXT）应用的包名（bundle name），整理成结构化的数据集，持续维护。

## 做什么 / 为什么

鸿蒙应用的包名没有公开的集中名录，逐个查找费时费力，网上流传的清单又常常把安卓包名混进来。本项目把这件事自动化：

- 以人工维护的应用名种子为起点，从华为应用市场公开接口匹配、核实每个应用的真实包名；
- 核实结果沉淀为结构化 JSON 数据集，任何人都可以直接取用；
- 由 GitHub Actions 每日自动更新，应用上架、改名、换包名后数据能跟上。

## 数据文件说明

| 路径 | 说明 |
| --- | --- |
| `seeds/include.txt`、`seeds/exclude.txt` | 分类种子：人工维护，一行一个应用名，`#` 开头为注释。两个文件是维护者按类别整理的两组常用应用名。种子里只写括注前的短名（如「花瓣搜索」「HMTG」）。 |
| `data/verified.json` | **唯一权威源**：已核实的鸿蒙包名（应用名 → bundle / fullName，新条目带 source / collectedAt / developer）。分发产物的包名只取它。 |
| `data/apps-pool.json` | 应用池产物（提交进仓）：榜单发现的常用应用；`bundle` 是应用市场 Web 目录包名（安卓目录，仅作对照），核实到的鸿蒙包名在 `hmBundle` 字段。 |
| `data/seed-check.json` | 种子对账报告（自动生成）：每个种子经接口组复核的状态（match / conflict / not-indexed）与证据；conflict 只上报、不自动改 verified。 |
| `data/pool-check.json` | 应用池核实报告（自动生成）：每个池内应用的鸿蒙包名核实证据。 |
| `dist/perapp-lists.json` | 最终分发产物（提交进仓）：已核实包名的结构化 JSON，见下节格式。 |
| `scripts/collect.py` | 采集脚本：读取种子、查应用市场接口、更新 `data/` 与 `dist/`。 |

## `dist/perapp-lists.json` 格式

```json
{"include":[{"name":"华为浏览器","bundle":"com.huawei.hmos.browser","verified":true}],"exclude":[...]}
```

- `include` / `exclude`：两个数组，对应种子文件的两个分类；
- `name`：应用名；
- `bundle`：应用包名（bundle name）；
- `verified`：包名是否已经过核实。

## 数据来源

- 包名核实走一组公开接口：`next.vcck.cn` 按名搜索（鸿蒙 NEXT 目录索引）发现候选，再经华为应用市场 `webedge/appinfo` 接口按包名 + 按 appId 双向确认；接口细节与实测结论见 [docs/api.md](docs/api.md)。
- 应用市场 Web 目录接口（getTabDetail）返回的是**安卓目录包名**，与鸿蒙包名大面积不一致，只用于榜单发现常用应用名，不作包名依据。
- 人工核实种子：`seeds/` 由维护者人工整理分类；新包名经接口确认 + 人工复核后才写入 `data/verified.json`。

## 自动更新

GitHub Actions（`.github/workflows/update.yml`）每日定时运行采集脚本，`dist/` 与 `data/` 有变化时自动提交回仓库；也可以在 Actions 页面手动触发（workflow_dispatch）。

## 贡献方式

- 增删条目、修正分类：改 `seeds/include.txt` / `seeds/exclude.txt`，提 PR；
- 发现包名错误或应用已改名 / 换包名：开 issue 说明应用名与正确包名（如有）。

## 许可证

- 代码：MIT，见 [LICENSE](LICENSE)。
- 数据文件（`dist/`、`data/` 下的 JSON）为包名等事实数据，供公众自由使用（CC0 意向）。

## 免责声明

- 本仓库中的包名为事实数据，仅供学习与研究参考。
- 包名可能随应用更新、改名或厂商调整而变化，本项目不保证与最新上架状态实时一致，请以应用市场实际信息为准。
- 文中应用名称及相关商标归各权利方所有，本项目与所列应用及其厂商无隶属或背书关系。

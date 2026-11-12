# harmonyos-app-lists

自动从华为应用市场采集热门 / 常用鸿蒙应用的包名，生成 [咪咪v2ray（MimiV2ray）](https://github.com/zhisibi/v2raymi) 分应用黑白名单可直接导入的 JSON。

## 做什么 / 为什么

咪咪v2ray 的分应用代理需要用户逐个勾选哪些应用走代理（白名单）、哪些应用直连（黑名单）。应用一多，手动查找、逐个勾选既费时又容易漏，包名更无从查起。

本项目把这件事自动化：

- 以人工维护的分类种子（应用名）为起点，从华为应用市场公开接口匹配、核实每个应用的真实包名（bundle name）；
- 生成一份可直接导入咪咪v2ray 的黑白名单 JSON，下载即用，不用再逐个勾选；
- 由 GitHub Actions 每日自动更新，应用上架、改名、换包名后名单能跟上。

## 数据文件说明

| 路径 | 说明 |
| --- | --- |
| `seeds/white.txt`、`seeds/black.txt` | 分类种子：人工维护，一行一个应用名，`#` 开头为注释。白名单 = 需要走代理的应用，黑名单 = 直连的国内常用应用。种子里只写括注前的短名（如「花瓣搜索」「HMTG」）。 |
| `data/apps-pool.json` | 应用池产物（提交进仓）：采集到的全部应用及其包名、核实状态等中间数据。 |
| `dist/perapp-lists.json` | 最终分发产物（提交进仓）：供咪咪v2ray 直接导入的黑白名单 JSON，见下节格式。 |
| `scripts/collect.py` | 采集脚本（后续提交补入）：读取种子、查应用市场、更新 `data/` 与 `dist/`。 |

## `dist/perapp-lists.json` 格式

```json
{"white":[{"name":"微信","bundle":"com.tencent.wechat","verified":true}],"black":[...]}
```

- `white` / `black`：两个数组，分别对应分应用白名单 / 黑名单；
- `name`：应用名；
- `bundle`：应用包名（bundle name）；
- `verified`：包名是否已经过核实。

## 在咪咪v2ray 中导入

1. 下载本仓库的 `dist/perapp-lists.json` 文件；
2. 打开咪咪v2ray，进入「分应用」页；
3. 点「导入 JSON」，选择刚下载的 `dist/perapp-lists.json` 文件；
4. 导入后白名单 / 黑名单即按文件内容生效，可再按需手动增减。

## 数据来源

- 华为应用市场公开接口：按种子应用名检索、匹配包名；
- 人工核实种子：`seeds/` 由维护者人工整理分类，核实状态以 `verified` 字段标注。

## 自动更新

GitHub Actions（`.github/workflows/update.yml`）每日定时运行采集脚本，`dist/` 与 `data/` 有变化时自动提交回仓库；也可以在 Actions 页面手动触发（workflow_dispatch）。

## 贡献方式

- 增删名单、修正分类：改 `seeds/white.txt` / `seeds/black.txt`，提 PR；
- 发现包名错误或应用已改名 / 换包名：开 issue 说明应用名与正确包名（如有）。

## 许可证

- 代码：MIT，见 [LICENSE](LICENSE)。
- 数据文件（`dist/`、`data/` 下的 JSON）为包名等事实数据，供公众自由使用（CC0 意向）。

## 免责声明

- 本仓库中的包名为事实数据，仅用于配置分应用代理名单。
- 包名可能随应用更新、改名或厂商调整而变化，本项目不保证与最新上架状态实时一致，请以应用市场实际信息为准。
- 文中应用名称及相关商标归各权利方所有，本项目与所列应用及其厂商无隶属或背书关系。

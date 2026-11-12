# AppGallery Web 接口说明（实测整理）

实测时间：2026-11-12。官网 SPA 的核心数据接口是 hispace 的
`internal.getTabDetail`（GET），搜索、详情、榜单共用同一个接口、靠 `uri`
区分。打通的关键是 interface-code 流程整套照抄官网 JS。

## 统一接口

- 业务接口：`GET {base}/uowap/index`
- 中国区 base：`https://web-drcn.hispace.dbankcloud.com/edge`
  （欧洲区 `https://web-dre.hispace.dbankcloud.com/edge`，流程相同；
  要中国区结果必须用 drcn，dre 的排序/召回是海外区结果。）
- 必需查询参数：`method`、`serviceType=20`、`uri`、`reqPageNum`、
  `maxResults=25`、`zone=`（未登录空串）、`locale=zh`。
  漏掉 `zone`/`locale` 会验码失败。
- 必需请求头：`Interface-Code`、`Identity-Id`（见下）、`Origin:
  https://appgallery.huawei.com`、`Referer: https://appgallery.huawei.com/`、
  与取码时一致的固定 UA。
- 返回通用结构：`rtnCode=0`；应用条目在 `layoutData[].dataList[]`，每条含
  `name`（应用名）、`package`（包名）、`appid`（C 开头应用 ID）、
  `detailId`、`icon`、`appVersionName` 等字段。

## 取码流程（interface-code 三件套）

1. 生成 `Identity-Id`：32 位十六进制、**无连字符**，全程复用同一个
   （带连字符的 UUID 会失败）。
2. 取码：`POST {base}/webedge/getInterfaceCode`
   - 请求头：`Interface-Code: null_<当前毫秒时间戳>`、`Identity-Id: <同上>`、
     `Origin`、`Referer`、固定 UA；
   - 请求体：`{"params":{},"zone":"","locale":"zh"}`，
     `Content-Type: application/json`；
   - 响应：200，JSON 字符串形式的 JWT（payload 的 `username` 即请求 UA
     原文，服务端会校验），**有效期 900 秒**。
3. 业务请求头：`Interface-Code: <JWT>_<当前毫秒时间戳>`、
   `Identity-Id: <与取码时同一个>`，UA 必须与取码时一致。
4. 验码失败返回 HTTP 403 + `{"rtnCode":1002,"rtnDesc":"InterfaceCode
   Verification failed."}`；客户端应自动重取码重试一次。

## 三个 uri

| 用途 | method / uri | 说明 |
| --- | --- | --- |
| 搜索 | `method=internal.getTabDetail`，`uri=searchApp\|<关键词>` | 结果在 `layoutData[].dataList[]`（多个 layout 分组，顺序即相关性顺序） |
| 详情 | `method=internal.getTabDetail`，`uri=app\|<appid>` | appid 只认 C 号（如 `C5683`），**不认包名**；字段在 `layoutData[layoutName="detailhiddencard"].dataList[0]`，含 `name`/`package`/`appid` |
| 榜单 | 先 `method=internal.getTemplate` 拿 `tabInfo[]` 的 tabId，再 `uri=<tabId>` 调 getTabDetail | Top tab 的 `tabEnName="Top"`；Categories tab 下有 Apps/Games 子 tab；翻页用 `reqPageNum` 递增，响应有 `hasNextPage` |

附带接口（已验码通过，但不作数据源）：热搜
`POST {base}/index/getnewhotsearchlist`（只有名称、无包名）；搜索补全
`POST {base}/index/completeSearchWord`（实测结果为空数组）。

## 注意事项：安卓目录包名 ≠ 鸿蒙包名

**本接口返回的是安卓目录的包名**，与鸿蒙原生应用的包名大面积不一致，
例如微信接口返回 `com.tencent.mm`，鸿蒙包名是 `com.tencent.wechat`；
QQ 返回 `com.tencent.mobileqq`，鸿蒙是 `com.tencent.mqq`；支付宝返回
`com.eg.android.AlipayGphone`，鸿蒙是 `com.alipay.mobile.client`。

因此本仓库的口径是：

- `data/verified.json`（人工核实的鸿蒙包名）是唯一权威源，
  `dist/perapp-lists.json` 的包名只取它；
- 接口搜索结果只用于对账（`data/seed-check.json`）和今后新种子的候选，
  候选一律 `verified=false`，不得直接进 dist；
- 榜单应用池 `data/apps-pool.json` 的包名来自本接口，每条带
  `bundleSource: "appgallery-web"` 标明是安卓目录包名。

## 详情页 HTML 无内嵌数据（结论）

`GET https://appgallery.huawei.com/app/detail?id=<包名>` 返回的只是
3,070 B 的 SPA 空壳（`<div id="app">`，标题为空，全页无应用名/包名），
HTML 内嵌信息这条路不存在，必须走上面的接口。另外 `uri=app|<包名>`
返回的是「已下架」页，只有推荐位、没有该应用本身的数据。

## 实跑补充（2026-11-12，scripts/collect.py 两轮实测）

- **搜索结果是宽松的推荐流，不是精确检索**：`searchApp|<关键词>` 返回
  `safeappcard` 分组的推荐应用集，目标应用经常不在结果内，且同一关键词
  两次运行的命中集不同（如「微信」一轮无、一轮首条命中；「QQ」相反）。
  结果里还混有近名异物（搜「花瓣搜索」混入「花瓣地图」、搜「QQ音乐」
  只有「QQ音乐HD」、搜「扫描全能王」命中的是第三方同名应用）。因此
  对账匹配必须宁缺毋滥：只认规范化名称全等或已知良性后缀，命中率低
  是接口特性，不代表种子有问题。
- **Top 榜 zh 为空、en 有数据**：drcn + `locale=zh` 请求 Top tabId 返回
  `layoutData: []`（count=0）；同一 tabId 改 `locale=en` 返回 15 条榜单
  （偏海外应用）。采集脚本 zh 取不到时回退 en，并在对账报告中注明。
- 分类 tab（Apps/Games）在 zh 下各返回 15 条，内容随运行有轮换，
  故应用池合并策略为旧条目保留并标 `stale: true`。

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
- 本接口（Web 目录）的搜索结果只用于发现应用名与粗对账参考，其包名
  不得进 verified/dist；包名核实一律走下文「在线获取包名」接口组；
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

---

# 「在线获取包名」接口组（鸿蒙目录，实测整理）

实测时间：2026-11-12。本组接口与上面的 Web 目录接口是两套
东西：这边返回的是**鸿蒙 NEXT 目录**的真实包名。collect.py 的种子对账与包名解析已改走本组接口。

## 接口一：按名搜索 next.vcck.cn（第三方索引，与 ClashBox 同源）

- `GET https://next.vcck.cn/api/apps/search-by-name?q=<关键词>&page=1&page_size=20`
- 返回 `{success, data: [{app_id, name, pkg_name, icon_url, version,
  developer_name, download_count_desc}], total}`；
- `app_id` 是长式 C 号（`C5765...`）；`pkg_name` 即鸿蒙包名；
  `icon_url` 路径含 `/phone/` 或 `/pc/`，可区分手机版 / PC 版；
- 性质：第三方索引、**覆盖不全**（华为系统应用、小众第三方客户端、
  部分银行未收录），「未收录」不等于应用不存在；同名结果里混有
  PC 版（包名 `.pc` 后缀）、手表版、元服务版（`com.atomicservice.*`），
  取本体须过滤，并经接口二确认后才算数。

## 接口二：AG webedge/appinfo 精确查（华为官方，Interface-Code 流程）

- 取码：`POST https://web-drcn.hispace.dbankcloud.com/edge/webedge/getInterfaceCode`，
  体 `{"params":{},"zone":"","locale":"zh"}`，请求头只需
  `Content-Type: application/json` 与固定 UA（JWT 与 UA 绑定，
  有效期约 900 秒；**本组流程不需要 Identity-Id**，与 Web 目录
  接口的取码三件套不同）；
- 查询：`POST .../edge/webedge/appinfo`，头
  `Interface-Code: <JWT>_<毫秒时间戳>`，UA 须与取码时一致，体二选一：
  - 按包名：`{pkgName: <包名>, appId: <包名>, locale: "zh_CN",
    countryCode: "CN"}` → 命中时响应 `pkgName` 与所查包名全等，
    附 `name` / `icon` / `developerName` 等；查无此应用返回空体；
  - 按 appId：`{pkgName: "", appId: <C号>, ...}` → 响应 `pkgName`
    即该应用的包名；
- **C 号有新旧两代，须认清来源**：vcck 给的长式 C 号属 NEXT 目录；
  老式短 C 号查到的是旧目录条目——实测 `C100170981` 返回
  `pkgName: "com.huawei.browser"`（旧版华为浏览器），与 NEXT 的
  `com.huawei.hmos.browser` 是两个条目。老式 `/app/C...` 分享链接
  按 appId 反查时可能落到旧条目，结果须再按包名复核。

## 可靠性验证（2026-11-12，作为采用前的硬门槛实跑）

- appinfo 按包名查 12 个已知应用（微信 / 淘宝 / 京东 / 小红书 /
  铁路12306 / 企业微信 / 支付宝 / QQ / 抖音 / 拼多多 / 哔哩哔哩 /
  华为浏览器）：**12/12 精确回显已核实鸿蒙包名**；
- vcck 按名搜同 12 个：10/12 的精确名命中中包含已核实包名
  （淘宝未进前 80 条结果、华为浏览器未被收录，均为覆盖问题，
  非数据错误）；
- 种子全量复核（97 个）：73 个 match、3 个 conflict（见下）、
  21 个索引未收录（含目录名与种子名不一致的个案，如优酷在目录中
  名为「优酷视频」，其包名 `com.youku.next` 已按目录名三向确认）；
- **conflict 只上报、绝不静默改 verified**。本轮 3 例（接口确认的
  同名应用与 verified 包名不同，且 verified 包名在目录里对应的是
  另一个应用）：
  - WPS Office：verified `cn.wps.mobileoffice.hap`（目录名为
    「WPS移动版」）vs 接口确认 `cn.wps.office.hap`；
  - 个人所得税：verified `cn.gov.chinatax.gt4.hm`（目录名为
    「电子税务局」）vs 接口确认 `cn.gov.tax.its.hm`；
  - 中国移动：verified `com.droi.tong`（目录名为「卓易通」）vs
    接口确认 `com.chinamobile.cmcc`。
  三例均待人工裁决，细节在 `data/seed-check.json`。

## 确认口径（collect.py 采用）

一个包名被接口组「确认」（confirmed）须同时满足：vcck 精确名命中、
图标为 phone 形态、非 PC/手表变体，且 appinfo 按包名与按 appId
两向返回的 `pkgName` 都等于候选、返回名与查询名全等。元服务版
（`com.atomicservice.*`）可能同时被确认，取本体时排除。

## 与分享链接提取工具的关系

开源工具 SummerKaze/HMOS-AppBundleNameForShared **不调用任何
接口**（未声明 INTERNET 权限）：它认定新式分享链接
`app/detail?id=` 后面就是包名、正则直取，与本组接口不是同源
关系；只有老式 `/app/C...` 链接里的 C 号才需要接口二反查。

## 本仓库口径（更新）

- `data/verified.json` 仍是 dist 的唯一权威源；
- 新包名进 verified 的标准路径：vcck 发现 + appinfo 双向确认 +
  人工复核后写入，条目带 `source: "vcck-search+ag-appinfo"`、
  `collectedAt`、`developer`；
- getTabDetail 在 collect.py 里只保留榜单发现应用名的用途；
  对账结果落 `data/seed-check.json`，应用池核实结果落
  `data/pool-check.json`，应用池条目以 `hmBundle` 字段标注核实到
  的鸿蒙包名（原 `bundle` 字段仍是 AG Web 的安卓目录包名）。

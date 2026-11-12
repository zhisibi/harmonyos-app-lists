#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""华为应用市场（AppGallery Web）采集脚本 —— 仅 Python 标准库。

数据口径（重要）：
  AG Web 接口返回的是**安卓目录包名**（如微信 com.tencent.mm），与已核实的
  鸿蒙包名（com.tencent.wechat）大面积不一致。因此：
  - data/verified.json 是唯一权威源，dist 的 bundle 只取它；
  - AG 搜索只用于种子对账（data/seed-check.json）与榜单应用池
    （data/apps-pool.json，条目带 bundleSource="appgallery-web" 标明来源）。

流程：
  1. interface-code 三件套取码（见 docs/api.md）；
  2. 逐个种子调 searchApp|<名> 对账 AG 包名 vs 已核实包名；
  3. getTemplate -> Top tab + 分类 tab（Apps/Games）各取一页榜单，汇总 pool；
  4. 按 seeds 顺序、只用 verified.json 生成 dist/perapp-lists.json；
  5. stdout 打印运行摘要。
"""

import http.cookiejar
import json
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = "https://web-drcn.hispace.dbankcloud.com/edge"  # 中国区
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
LOCALE = "zh"
THROTTLE = 0.45        # 每请求最小间隔（秒），要求 >= 0.4
CODE_TTL = 800         # interface-code 有效期 900s，提前刷新
SEARCH_RETRIES = 2     # 搜索失败重试上限


# ---------------------------------------------------------------- HTTP 层
class AgClient:
    """照抄官网 JS 的 interface-code 流程（实测细节见 docs/api.md）。"""

    def __init__(self):
        cj = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(cj))
        # Identity-Id：32 位无连字符 hex，全程复用同一个
        self.ident = secrets.token_hex(16)
        self.code = None
        self.code_at = 0.0
        self._last_req = 0.0

    def _throttle(self):
        wait = THROTTLE - (time.time() - self._last_req)
        if wait > 0:
            time.sleep(wait)
        self._last_req = time.time()

    def _raw(self, method, url, headers=None, data=None):
        h = {"User-Agent": UA,
             "Referer": "https://appgallery.huawei.com/",
             "Origin": "https://appgallery.huawei.com",
             "Accept": "application/json, text/plain, */*"}
        if headers:
            h.update(headers)
        req = urllib.request.Request(url, headers=h, data=data, method=method)
        self._throttle()
        try:
            with self.opener.open(req, timeout=25) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except Exception as e:  # 网络异常按失败返回，交给上层重试
            return None, str(e).encode()

    def fetch_code(self):
        """POST /webedge/getInterfaceCode，取码时 Interface-Code 填 null_<ms>。"""
        hdr = {"Content-Type": "application/json;charset=UTF-8",
               "Interface-Code": f"null_{int(time.time() * 1000)}",
               "Identity-Id": self.ident}
        body = json.dumps({"params": {}, "zone": "", "locale": LOCALE}).encode()
        st, raw = self._raw("POST", BASE + "/webedge/getInterfaceCode", hdr, body)
        if st != 200:
            raise RuntimeError(f"getInterfaceCode failed: HTTP {st}")
        self.code = json.loads(raw)
        self.code_at = time.time()

    def _auth_headers(self):
        if self.code is None or time.time() - self.code_at > CODE_TTL:
            self.fetch_code()
        return {"Interface-Code": f"{self.code}_{int(time.time() * 1000)}",
                "Identity-Id": self.ident}

    def get_json(self, params):
        """GET /uowap/index；遇 403 / rtnCode=1002 自动重取码重试一次。"""
        url = BASE + "/uowap/index?" + urllib.parse.urlencode(params)
        for attempt in (0, 1):
            st, raw = self._raw("GET", url, self._auth_headers())
            data = None
            if st == 200:
                try:
                    data = json.loads(raw)
                except ValueError:
                    data = None
            denied = (st == 403) or (isinstance(data, dict)
                                     and data.get("rtnCode") == 1002)
            if denied and attempt == 0:
                self.fetch_code()
                continue
            if st != 200 or data is None:
                raise RuntimeError(f"GET failed: HTTP {st}")
            return data
        raise RuntimeError("GET failed after code refresh")

    def get_tab_detail(self, uri, page=1, locale=None):
        return self.get_json({"method": "internal.getTabDetail",
                              "serviceType": 20, "reqPageNum": page,
                              "uri": uri, "maxResults": 25,
                              "zone": "", "locale": locale or LOCALE})

    def get_template(self):
        return self.get_json({"method": "internal.getTemplate",
                              "serviceType": 20, "zone": "", "locale": LOCALE})


def extract_apps(payload):
    """从 getTabDetail 响应里按顺序抽出 layoutData[].dataList[] 的应用条目。"""
    apps = []
    for layout in payload.get("layoutData") or []:
        for item in layout.get("dataList") or []:
            name, pkg = item.get("name"), item.get("package")
            if name and pkg:
                apps.append({"name": name, "bundle": pkg})
    return apps


# ---------------------------------------------------------------- 种子对账
def pick_best(seed, apps):
    """取与种子名可信对应的结果，宁缺毋滥。

    实测教训：本接口的搜索返回的是宽松的 safeappcard 推荐流，目标应用
    经常不在结果里，且混有大量近名异物（如搜「花瓣搜索」混入「花瓣地图」、
    搜「QQ音乐」只有「QQ音乐HD」）。用包含/相似度匹配会把异物当成本体，
    污染对账，故只接受：
      1. 规范化（去空格、忽略大小写）后名称全等；
      2. 名称以种子名开头且尾巴是已知良性后缀（OCR / -PDF,…，如
         「扫描全能王OCR」「WPS Office-PDF,Word,Excel,PPT」）。
    其余一律视为未找到（返回 None），不用异物包名冒充对账结果。
    """
    if not apps:
        return None
    norm = lambda s: s.replace(" ", "").lower()
    nseed = norm(seed)
    for a in apps:
        if norm(a["name"]) == nseed:
            return a
    for a in apps:
        nname = norm(a["name"])
        if nname.startswith(nseed):
            rest = nname[len(nseed):].lstrip("-")
            if rest == "ocr" or rest.startswith("pdf"):
                return a
    return None


def search_seed(client, seed):
    """搜索单个种子，失败重试 <= SEARCH_RETRIES 次。

    返回 (最佳可信匹配, 结果首条)；都可能为 None。
    """
    last_err = None
    for _ in range(SEARCH_RETRIES + 1):
        try:
            payload = client.get_tab_detail(f"searchApp|{seed}")
            apps = extract_apps(payload)
            return pick_best(seed, apps), (apps[0] if apps else None)
        except RuntimeError as e:
            last_err = e
            time.sleep(1.0)
    print(f"  [warn] search failed for {seed}: {last_err}", file=sys.stderr)
    return None, None


def read_seeds(name):
    path = ROOT / "seeds" / f"{name}.txt"
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")]


# ---------------------------------------------------------------- 榜单采集
def find_tabs(template):
    """从 getTemplate 的 tabInfo 里找 Top tab 与分类子 tab（Apps/Games）。"""
    top_id, categories = None, []

    def walk(tabs):
        nonlocal top_id
        for tab in tabs or []:
            label = tab.get("tabEnName") or tab.get("tabName") or ""
            if label == "Top" and top_id is None:
                top_id = tab.get("tabId")
            if label == "Categories":
                for child in tab.get("tabInfo") or []:
                    cl = child.get("tabEnName") or child.get("tabName") or ""
                    if cl in ("Apps", "Games"):
                        categories.append((cl, child.get("tabId")))
            walk(tab.get("tabInfo"))

    walk(template.get("tabInfo"))
    return top_id, categories


def collect_pool(client):
    """Top 总榜 + 主要分类各一页，按 bundle 去重保留最高排名。"""
    template = client.get_template()
    top_id, categories = find_tabs(template)
    sources = []
    if top_id:
        sources.append(("Top", top_id))
    sources.extend(categories)

    best = {}  # bundle -> entry（rank 越小越好）
    for label, tab_id in sources:
        try:
            apps = extract_apps(client.get_tab_detail(tab_id))
            if label == "Top" and not apps:
                # 实测：drcn + locale=zh 的 Top 页 layoutData 为空，
                # 同 tabId 用 locale=en 才有榜单数据，故回退一次。
                apps = extract_apps(client.get_tab_detail(tab_id, locale="en"))
        except RuntimeError as e:
            print(f"  [warn] ranking {label} failed: {e}", file=sys.stderr)
            continue
        for idx, a in enumerate(apps, start=1):
            cur = best.get(a["bundle"])
            if cur is None or idx < cur["rank"]:
                best[a["bundle"]] = {"name": a["name"], "bundle": a["bundle"],
                                     "rank": idx, "from": label,
                                     "bundleSource": "appgallery-web"}
    return sorted(best.values(), key=lambda e: (e["rank"], e["bundle"]))


def merge_pool(old, new):
    """与上次 pool 合并：旧条目本次没抓到不删，标 stale=true。"""
    new_bundles = {e["bundle"] for e in new}
    merged = list(new)
    for e in old or []:
        if e.get("bundle") not in new_bundles:
            stale = dict(e)
            stale["stale"] = True
            merged.append(stale)
    return merged


# ---------------------------------------------------------------- 主流程
def main():
    verified = json.loads((ROOT / "data" / "verified.json").read_text("utf-8"))
    seeds = {cat: read_seeds(cat) for cat in ("white", "black")}
    client = AgClient()

    # 1) 种子对账：AG 搜索包名 vs 已核实包名
    check, match, conflict, unresolved = [], 0, 0, []
    total = sum(len(v) for v in seeds.values())
    done = 0
    for cat in ("white", "black"):
        for seed in seeds[cat]:
            done += 1
            v = verified.get(seed)
            if v is None:
                unresolved.append(seed)  # 无权威源，不用 AG 包名冒充
            hit, top = search_seed(client, seed)
            ag_bundle = hit["bundle"] if hit else None
            same = (ag_bundle is not None and v is not None
                    and ag_bundle == v["bundle"])
            if v is not None:
                if same:
                    match += 1
                elif ag_bundle is not None:
                    conflict += 1
            check.append({"seed": seed, "category": cat,
                          "agName": hit["name"] if hit else None,
                          "agBundle": ag_bundle,
                          "agTopName": top["name"] if top else None,
                          "agTopBundle": top["bundle"] if top else None,
                          "verifiedBundle": v["bundle"] if v else None,
                          "match": same if v is not None else None})
            print(f"  [{done}/{total}] {seed}: AG={ag_bundle} "
                  f"verified={v['bundle'] if v else None} match={same}")
    (ROOT / "data" / "seed-check.json").write_text(
        json.dumps(check, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    # 2) 榜单采集 + 与旧 pool 合并
    pool_path = ROOT / "data" / "apps-pool.json"
    old_pool = (json.loads(pool_path.read_text("utf-8"))
                if pool_path.exists() else [])
    pool = merge_pool(old_pool, collect_pool(client))
    pool_path.write_text(json.dumps(pool, ensure_ascii=False, indent=1) + "\n",
                         encoding="utf-8")

    # 3) dist：只取 verified.json，顺序同 seeds，解析不到的不进表
    dist = {}
    for cat in ("white", "black"):
        dist[cat] = [{"name": s, "bundle": verified[s]["bundle"],
                      "verified": True}
                     for s in seeds[cat] if s in verified]
    (ROOT / "dist" / "perapp-lists.json").write_text(
        json.dumps(dist, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    # 4) 摘要
    print("=== summary ===")
    print(f"pool entries: {len(pool)}")
    print(f"seed check: match={match} conflict={conflict} "
          f"total_verified={match + conflict}")
    print(f"seed check: ag_not_found="
          f"{sum(len(v) for v in seeds.values()) - len(unresolved) - match - conflict}")
    print(f"unresolved seeds: {len(unresolved)} {unresolved}")
    print(f"dist: white={len(dist['white'])} black={len(dist['black'])}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""华为应用市场采集脚本 —— 仅 Python 标准库。

数据口径（重要，2026-11-12 改版）：
  本仓库涉及两个完全不同的接口，返回的包名不是一回事：
  1. AG Web 目录接口（getTabDetail / getTemplate）：返回**安卓目录包名**
     （微信 com.tencent.mm），与鸿蒙包名大面积不一致（实测 0/15）。
     本脚本里它只用于榜单采集——发现「哪些应用常用」的名字，包名仅作
     对照记录（bundleSource="appgallery-web"），永不作数。
  2. 「在线获取包名」接口组（咪咪v2ray App 内同源，见 docs/api.md）：
     - next.vcck.cn 按名搜索：索引的是鸿蒙 NEXT 目录，返回候选
       （app_id + pkg_name，pkg_name 即鸿蒙包名）；
     - AG webedge/appinfo 精确查（Interface-Code 流程）：按包名查必须
       原样回显该包名、按 appId 查返回其 pkgName，双向都对上才算确认。
     已用 12 个已知应用验证：appinfo 按包名 12/12 精确回显已核实鸿蒙
     包名；vcck 精确名命中里 10/12 含已核实包名（未收录 ≠ 冲突）。

  - data/verified.json 是唯一权威源，dist 的 bundle 只取它；
  - 新包名进 verified.json 的唯一路径：vcck 发现 + appinfo 双向确认 +
    人工/多源复核后手动写入（带 source / collectedAt / developer）；
  - 对账发现的冲突只记录在 data/seed-check.json，绝不静默改 verified。

流程：
  1. 种子对账：逐个种子走 vcck 搜索 + appinfo 确认，与 verified 比对，
     结果写 data/seed-check.json（match / conflict / not-indexed）；
  2. 榜单采集：getTemplate -> Top + 分类（Apps/Games）各一页，汇入应用池
     （data/apps-pool.json），再逐个用新接口核实鸿蒙包名，核实结果写
     data/pool-check.json 并以 hmBundle 字段标注回应用池；
  3. 按 seeds 顺序、只用 verified.json 生成 dist/perapp-lists.json；
  4. stdout 打印运行摘要。
"""

import http.cookiejar
import json
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = "https://web-drcn.hispace.dbankcloud.com/edge"  # 中国区
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
LOCALE = "zh"
THROTTLE = 0.45        # AG Web 每请求最小间隔（秒），要求 >= 0.4
CODE_TTL = 800         # interface-code 有效期 900s，提前刷新
SEARCH_RETRIES = 2     # 搜索失败重试上限

# --- 「在线获取包名」接口组（与咪咪v2ray AppSearchNet.ets 同源同 UA） ---
HM_BASE = BASE + "/webedge/"
VCCK_SEARCH = "https://next.vcck.cn/api/apps/search-by-name"
HM_UA = "Mozilla/5.0 (Linux; HarmonyOS) v2rayMi"   # appinfo 的 JWT 与此 UA 绑定
VCCK_UA = "v2rayMi"
HM_THROTTLE = 0.35
TODAY = date.today().isoformat()


# ---------------------------------------------------------------- HTTP 层
class AgClient:
    """照抄官网 JS 的 interface-code 流程（实测细节见 docs/api.md）。

    只用于 getTabDetail / getTemplate（榜单发现应用名）；其返回的包名
    是安卓目录包名，不作核实依据。
    """

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


# ------------------------------------------------- 在线获取包名（鸿蒙目录）
class HmClient:
    """vcck 按名搜索 + AG webedge/appinfo 精确查（流程同 v2raymi App 内）。

    - vcck 索引的是鸿蒙 NEXT 目录：search-by-name 返回候选的 pkg_name
      就是鸿蒙包名，但它是第三方索引、覆盖不全，未收录不等于不存在；
    - appinfo 是精确核验器：按包名查，响应 pkgName 必须与所查包名全等；
      按 appId 查，响应 pkgName 即该应用的包名。候选须两向都对上、
      且 appinfo 返回名与查询名全等，才计 confirmed。
    """

    def __init__(self):
        self.code = None
        self.code_at = 0.0
        self._last_req = 0.0

    def _throttle(self):
        wait = HM_THROTTLE - (time.time() - self._last_req)
        if wait > 0:
            time.sleep(wait)
        self._last_req = time.time()

    def _raw(self, url, method="GET", body=None, headers=None, ua=HM_UA):
        h = {"User-Agent": ua, "Accept": "application/json"}
        if headers:
            h.update(headers)
        data = body.encode() if isinstance(body, str) else body
        req = urllib.request.Request(url, data=data, headers=h, method=method)
        self._throttle()
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
        except Exception as e:
            return None, f"ERR {e}"

    def _fetch_code(self):
        st, raw = self._raw(HM_BASE + "getInterfaceCode", "POST",
                            '{"params":{},"zone":"","locale":"zh"}',
                            {"Content-Type": "application/json"})
        if st != 200:
            raise RuntimeError(f"hm getInterfaceCode HTTP {st}")
        code = json.loads(raw)
        if not isinstance(code, str) or len(code) < 40:
            raise RuntimeError("hm getInterfaceCode: bad body")
        self.code, self.code_at = code, time.time()

    def _code(self):
        if self.code is None or time.time() - self.code_at > 600:
            self._fetch_code()
        return self.code

    def appinfo(self, pkg_name, app_id):
        """POST appinfo；返回解析后的 dict，查无此应用时带 empty=True。"""
        for attempt in (0, 1):
            try:
                jwt = self._code()
            except RuntimeError:
                self.code = None
                if attempt == 0:
                    continue
                return {"error": "no interface code"}
            body = json.dumps({"pkgName": pkg_name, "appId": app_id,
                               "locale": "zh_CN", "countryCode": "CN"})
            st, raw = self._raw(
                HM_BASE + "appinfo", "POST", body,
                {"Content-Type": "application/json",
                 "Interface-Code": f"{jwt}_{int(time.time() * 1000)}"})
            if st == 200 and raw.strip():
                try:
                    return json.loads(raw)
                except ValueError:
                    return {"error": "bad json"}
            if st == 200:
                return {"empty": True}
            self.code = None
            time.sleep(1.0)
        return {"error": "appinfo failed"}

    def vcck_pages(self, q, max_pages=4):
        """逐页拉 vcck 搜索结果（page_size=20），命中精确名或翻完即停。"""
        out, total = [], None
        for page in range(1, max_pages + 1):
            url = VCCK_SEARCH + "?" + urllib.parse.urlencode(
                {"q": q, "page": page, "page_size": 20})
            got = None
            for _ in range(SEARCH_RETRIES + 1):
                st, raw = self._raw(url, ua=VCCK_UA)
                if st == 200:
                    try:
                        got = json.loads(raw)
                        break
                    except ValueError:
                        pass
                time.sleep(1.2)
            if not got or not got.get("success"):
                break
            data = got.get("data") or []
            out.extend(data)
            total = got.get("total", total)
            if any(norm(it.get("name")) == norm(q) for it in data):
                break
            if not data or (total is not None and len(out) >= total):
                break
            time.sleep(HM_THROTTLE)
        return out, total


def norm(s):
    return (s or "").replace(" ", "").replace("　", "").lower()


def _is_variant(pkg, icon):
    """同名异物：PC 版、手表版不算手机本体候选。"""
    p = (pkg or "").lower()
    if p.endswith(".pc") or ".pc." in p or "watch" in p:
        return True
    return bool(icon) and "/pc/" in icon


def resolve_name(hm, name):
    """按名解析一个应用，返回带完整证据的记录（不做任何写库决定）。

    confirmed 的判定（与 2026-11-12 验证轮一致）：候选来自 vcck 精确名
    命中、图标为 phone 形态、非 PC/手表变体，且 appinfo 按包名与按
    appId 两向返回的 pkgName 都等于候选、返回名与查询名全等。
    元服务版（com.atomicservice.*）会一并出现在 confirmed 里，取本体时
    须排除（见 pick_main_bundle）。
    """
    rec = {"query": name}
    data, total = hm.vcck_pages(name)
    rec["vcckTotal"] = total
    exact = [it for it in data if norm(it.get("name")) == norm(name)]
    rec["exactHits"] = [
        {"name": it.get("name"), "pkgName": it.get("pkg_name"),
         "appId": it.get("app_id"), "developerName": it.get("developer_name"),
         "iconUrl": it.get("icon_url"), "version": it.get("version")}
        for it in exact]
    phones = [it for it in exact
              if it.get("icon_url") and "/phone/" in it["icon_url"]
              and not _is_variant(it.get("pkg_name"), it.get("icon_url"))]
    rec["phoneCandidates"] = [it.get("pkg_name") for it in phones]
    confs = []
    for it in phones[:3]:
        cand = it.get("pkg_name")
        by_bundle = hm.appinfo(cand, cand)
        time.sleep(HM_THROTTLE)
        by_id = hm.appinfo("", str(it.get("app_id") or ""))
        time.sleep(HM_THROTTLE)
        confs.append({
            "bundle": cand, "appId": it.get("app_id"),
            "developerName": it.get("developer_name"),
            "byBundlePkg": by_bundle.get("pkgName"),
            "byBundleName": by_bundle.get("name"),
            "byIdPkg": by_id.get("pkgName"),
            "byIdName": by_id.get("name")})
    rec["confirmations"] = confs
    rec["confirmed"] = [
        c["bundle"] for c in confs
        if c["byBundlePkg"] == c["bundle"] and c["byIdPkg"] == c["bundle"]
        and c["byBundleName"] and norm(c["byBundleName"]) == norm(name)]
    return rec


def pick_main_bundle(confirmed):
    """confirmed 里取本体包名：排除元服务（com.atomicservice.*）后的第一条。"""
    for b in confirmed or []:
        if not b.startswith("com.atomicservice."):
            return b
    return None


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
    """Top 总榜 + 主要分类各一页，按 bundle 去重保留最高排名。

    注意：这里的 bundle 是 AG Web 的安卓目录包名，只用于发现应用名与
    对照记录；鸿蒙包名由 resolve_name 另行核实（hmBundle）。
    """
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
    """与上次 pool 合并：旧条目本次没抓到不删，标 stale=true。

    旧条目上的 hmBundle 等核实标注随条目保留，resolve 阶段会刷新。
    """
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
    ag = AgClient()
    hm = HmClient()

    # 1) 种子对账：vcck 搜索 + appinfo 确认 vs verified（只记录，不改 verified）
    check, n_match, n_conflict, n_absent = [], 0, 0, 0
    total = sum(len(v) for v in seeds.values())
    done = 0
    for cat in ("white", "black"):
        for seed in seeds[cat]:
            done += 1
            v = verified.get(seed)
            v_bundle = v["bundle"] if v else None
            try:
                rec = resolve_name(hm, seed)
            except Exception as e:  # 单条失败不中断全轮
                print(f"  [warn] resolve failed for {seed}: {e}",
                      file=sys.stderr)
                rec = {"query": seed, "exactHits": [], "phoneCandidates": [],
                       "confirmations": [], "confirmed": [], "fatal": str(e)}
            confirmed = rec.get("confirmed", [])
            if v_bundle is not None and v_bundle in confirmed:
                status = "match"
                n_match += 1
            elif confirmed:
                status = "conflict"  # 接口确认了别的包名：只上报，不改 verified
                n_conflict += 1
            else:
                status = "not-indexed"  # 索引未收录，不等于 verified 有问题
                n_absent += 1
            entry = {"seed": seed, "category": cat,
                     "verifiedBundle": v_bundle,
                     "confirmedBundles": confirmed,
                     "status": status,
                     "exactHits": rec.get("exactHits", []),
                     "confirmations": rec.get("confirmations", [])}
            if status == "conflict" and v_bundle is not None:
                # 附带查清 verified 包名在目录里到底是谁，供人工裁决
                info = hm.appinfo(v_bundle, v_bundle)
                entry["verifiedBundleName"] = info.get("name")
                time.sleep(HM_THROTTLE)
            check.append(entry)
            print(f"  [{done}/{total}] {seed}: status={status} "
                  f"confirmed={confirmed} verified={v_bundle}")
    (ROOT / "data" / "seed-check.json").write_text(
        json.dumps({"interface": "vcck-search+ag-appinfo",
                    "generatedAt": TODAY, "results": check},
                   ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    # 2) 榜单采集（AG Web 只发现名字）+ 与旧 pool 合并 + 鸿蒙包名核实
    pool_path = ROOT / "data" / "apps-pool.json"
    old_pool = (json.loads(pool_path.read_text("utf-8"))
                if pool_path.exists() else [])
    pool = merge_pool(old_pool, collect_pool(ag))
    pool_check, n_pool_ok = [], 0
    for e in pool:
        try:
            rec = resolve_name(hm, e["name"])
        except Exception as ex:
            print(f"  [warn] pool resolve failed for {e['name']}: {ex}",
                  file=sys.stderr)
            rec = {"confirmed": [], "exactHits": [], "confirmations": []}
        main_bundle = pick_main_bundle(rec.get("confirmed", []))
        if main_bundle:
            e["hmBundle"] = main_bundle
            e["hmBundleSource"] = "vcck-search+ag-appinfo"
            n_pool_ok += 1
        else:
            e.pop("hmBundle", None)
            e.pop("hmBundleSource", None)
        pool_check.append({
            "name": e["name"], "agwebBundle": e["bundle"],
            "hmBundle": main_bundle,
            "confirmedBundles": rec.get("confirmed", []),
            "exactHits": rec.get("exactHits", []),
            "confirmations": rec.get("confirmations", [])})
        print(f"  [pool] {e['name']}: hmBundle={main_bundle}")
    pool_path.write_text(json.dumps(pool, ensure_ascii=False, indent=1) + "\n",
                         encoding="utf-8")
    (ROOT / "data" / "pool-check.json").write_text(
        json.dumps({"interface": "vcck-search+ag-appinfo",
                    "generatedAt": TODAY, "results": pool_check},
                   ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

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
    print(f"seed check: match={n_match} conflict={n_conflict} "
          f"not-indexed={n_absent} total={total}")
    for e in check:
        if e["status"] == "conflict":
            print(f"  conflict: {e['seed']} verified={e['verifiedBundle']} "
                  f"({e.get('verifiedBundleName')}) "
                  f"confirmed={e['confirmedBundles']}")
    print(f"pool entries: {len(pool)} hmBundle confirmed: {n_pool_ok}")
    print(f"dist: white={len(dist['white'])} black={len(dist['black'])}")


if __name__ == "__main__":
    main()

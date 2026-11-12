#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""intake.py 离线自测 —— FakeHM 模拟 vcck/appinfo，不出网。

覆盖口径：
  - 无冲突过闸 -> accepted；完全一致 -> already-verified（不查接口）；
  - 过闸后冲突以提交为准（2026-11-12 裁决）：同名换包名 / 同包名改名 /
    同名同包名移分类 / 名与包名双向交叉冲突，均 overridden 并留审计；
  - 过不了闸一律拒绝（vcck-no-exact-phone-hit / name-mismatch 等
    stage 不变），裁决不覆盖闸口；非法条目 stage=error；
  - 不再产生 conflict-existing 结局；
  - remove_seed 保注释删行、append_overrides 建档追加。

运行：python3 scripts/test_intake.py（全过退出 0，失败退出 1 并打印明细）。
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import intake  # noqa: E402

FAILURES = []


def check(label, cond, extra=""):
    if cond:
        print(f"  ok: {label}")
    else:
        print(f"  FAIL: {label} {extra}")
        FAILURES.append(label)


# ---------------------------------------------------------------- FakeHM
class FakeHM:
    """registry: 查询名 -> [候选 dict]。

    候选键：pkg（包名）、app_id、dev、hit_name（vcck 命中名，缺省=查询名）、
    by_bundle_name（appinfo 按包名返回名，缺省=hit_name）、
    by_bundle_pkg / by_id_pkg（缺省=pkg）。
    """

    def __init__(self, registry):
        self.registry = registry
        self.calls = 0

    def vcck_pages(self, q, max_pages=4):
        self.calls += 1
        items = []
        for cand in self.registry.get(q, []):
            items.append({
                "name": cand.get("hit_name", q),
                "pkg_name": cand["pkg"],
                "app_id": cand.get("app_id", "id-" + cand["pkg"]),
                "developer_name": cand.get("dev", "测试公司"),
                "icon_url": "https://example.com/phone/icon.png",
                "version": "1.0",
            })
        return items, len(items)

    def _find(self, key, value):
        for q, cands in self.registry.items():
            for cand in cands:
                if cand.get(key) == value or (
                        key == "pkg" and cand["pkg"] == value) or (
                        key == "app_id"
                        and cand.get("app_id", "id-" + cand["pkg"]) == value):
                    return q, cand
        return None, None

    def appinfo(self, pkg_name, app_id):
        self.calls += 1
        if pkg_name:
            q, cand = self._find("pkg", pkg_name)
            if cand is None:
                return {}
            return {"pkgName": cand.get("by_bundle_pkg", cand["pkg"]),
                    "name": cand.get("by_bundle_name",
                                      cand.get("hit_name", q))}
        q, cand = self._find("app_id", app_id)
        if cand is None:
            return {}
        return {"pkgName": cand.get("by_id_pkg", cand["pkg"]),
                "name": cand.get("by_bundle_name",
                                  cand.get("hit_name", q))}


# ---------------------------------------------------------------- 夹具
BASE_VERIFIED = {
    "甲应用": {"bundle": "com.a.old", "fullName": "甲应用"},
    "乙应用": {"bundle": "com.b.app", "fullName": "乙应用"},
    "丙应用": {"bundle": "com.c.app", "fullName": "丙应用"},
    "戊应用": {"bundle": "com.e.old", "fullName": "戊应用"},
    "己应用": {"bundle": "com.f.one", "fullName": "己应用"},
    "庚应用": {"bundle": "com.g.two", "fullName": "庚应用"},
}
BASE_SEEDS = {
    "include": ["丙应用"],
    "exclude": ["甲应用", "乙应用", "戊应用", "己应用", "庚应用"],
}

_TMP = tempfile.TemporaryDirectory()
_TMPDIR = Path(_TMP.name)


def run_case(items, registry):
    """以全新夹具跑一轮 process_items，返回全部产物。"""
    vpath = _TMPDIR / "verified.json"
    vpath.write_text(json.dumps(BASE_VERIFIED, ensure_ascii=False),
                     encoding="utf-8")
    intake.VERIFIED_PATH = vpath
    seeds_copy = {k: list(v) for k, v in BASE_SEEDS.items()}
    intake.read_seeds = lambda cat: list(seeds_copy[cat])
    hm = FakeHM(registry)
    out = intake.process_items(items, hm=hm)
    return out, hm


def by_name(results, name):
    for r in results:
        if r["name"] == name:
            return r
    return None


# ---------------------------------------------------------------- 用例
def case_accepted():
    print("case: 无冲突新入选")
    (results, verified, seed_ops, rejected, overrides), _ = run_case(
        [{"name": "丁应用", "bundle": "com.d.app", "list": "black"}],
        {"丁应用": [{"pkg": "com.d.app", "dev": "丁公司"}]})
    r = by_name(results, "丁应用")
    check("outcome=accepted", r["outcome"] == "accepted", str(r))
    check("verified 新增且带 app-submit 来源",
          verified["丁应用"]["bundle"] == "com.d.app"
          and verified["丁应用"]["source"] == intake.SOURCE_TAG)
    check("seed_ops 追加 exclude", ("append", "exclude", "丁应用") in seed_ops,
          str(seed_ops))
    check("无 overrides/rejected", not overrides and not rejected)


def case_override_bundle():
    print("case: 同名不同包名 -> 覆盖换包名")
    (results, verified, seed_ops, rejected, overrides), _ = run_case(
        [{"name": "甲应用", "bundle": "com.a.new", "list": "black"}],
        {"甲应用": [{"pkg": "com.a.new"}]})
    r = by_name(results, "甲应用")
    check("outcome=overridden", r["outcome"] == "overridden", str(r))
    check("bundle 已换", verified["甲应用"]["bundle"] == "com.a.new")
    check("source 更新为 app-submit 口径",
          verified["甲应用"]["source"] == intake.SOURCE_TAG)
    check("审计一条且字段对", len(overrides) == 1
          and overrides[0]["oldBundle"] == "com.a.old"
          and overrides[0]["newBundle"] == "com.a.new"
          and overrides[0]["oldList"] == "exclude"
          and overrides[0]["newList"] == "exclude"
          and overrides[0]["origin"] == "app-submit", str(overrides))
    check("分类不变则无 seed_ops", seed_ops == [], str(seed_ops))
    check("无 rejected", not rejected)


def case_override_rename():
    print("case: 同包名不同名 -> 覆盖改名")
    (results, verified, seed_ops, rejected, overrides), _ = run_case(
        [{"name": "乙应用新名", "bundle": "com.b.app", "list": "black"}],
        {"乙应用新名": [{"pkg": "com.b.app"}]})
    r = by_name(results, "乙应用新名")
    check("outcome=overridden", r["outcome"] == "overridden", str(r))
    check("旧名条目已删、新名条目在",
          "乙应用" not in verified
          and verified["乙应用新名"]["bundle"] == "com.b.app")
    check("seed_ops 先删旧名再加新名",
          seed_ops == [("remove", "exclude", "乙应用"),
                       ("append", "exclude", "乙应用新名")], str(seed_ops))
    check("审计带 oldName", len(overrides) == 1
          and overrides[0].get("oldName") == "乙应用"
          and overrides[0]["oldBundle"] == "com.b.app"
          and overrides[0]["newBundle"] == "com.b.app", str(overrides))


def case_override_category():
    print("case: 同名同包名不同分类 -> 移分类")
    (results, verified, seed_ops, rejected, overrides), _ = run_case(
        [{"name": "丙应用", "bundle": "com.c.app", "list": "black"}],
        {"丙应用": [{"pkg": "com.c.app"}]})
    r = by_name(results, "丙应用")
    check("outcome=overridden", r["outcome"] == "overridden", str(r))
    check("条目本身不动", verified["丙应用"] == BASE_VERIFIED["丙应用"])
    check("seed_ops 移分类",
          seed_ops == [("remove", "include", "丙应用"),
                       ("append", "exclude", "丙应用")], str(seed_ops))
    check("审计 oldList/newList",
          len(overrides) == 1 and overrides[0]["oldList"] == "include"
          and overrides[0]["newList"] == "exclude", str(overrides))


def case_already_verified():
    print("case: 完全一致 -> already-verified 且不查接口")
    (results, verified, seed_ops, rejected, overrides), hm = run_case(
        [{"name": "乙应用", "bundle": "com.b.app", "list": "black"}], {})
    r = by_name(results, "乙应用")
    check("outcome=already-verified",
          r["outcome"] == "already-verified", str(r))
    check("未调用接口", hm.calls == 0, f"calls={hm.calls}")
    check("无任何写操作", not seed_ops and not overrides and not rejected
          and verified == BASE_VERIFIED)


def case_gate_rejects_fake_bundle():
    print("case: 同名提交假包名 -> 过不了闸仍拒绝")
    (results, verified, seed_ops, rejected, overrides), _ = run_case(
        [{"name": "甲应用", "bundle": "com.a.fake", "list": "black"}],
        {"甲应用": [{"pkg": "com.a.new"}]})
    r = by_name(results, "甲应用")
    check("outcome=rejected", r["outcome"] == "rejected", str(r))
    check("stage=vcck-no-exact-phone-hit",
          r.get("stage") == "vcck-no-exact-phone-hit", str(r))
    check("verified 未被动", verified == BASE_VERIFIED)
    check("无 overrides", not overrides)


def case_gate_rejects_name_mismatch():
    print("case: 平台回显名不符 -> name-mismatch 拒绝（裁决不覆盖闸口）")
    (results, verified, seed_ops, rejected, overrides), _ = run_case(
        [{"name": "戊应用", "bundle": "com.e.app", "list": "black"}],
        {"戊应用": [{"pkg": "com.e.app", "by_bundle_name": "戊应用Pro"}]})
    r = by_name(results, "戊应用")
    check("outcome=rejected", r["outcome"] == "rejected", str(r))
    check("stage=name-mismatch", r.get("stage") == "name-mismatch", str(r))
    check("verified 未被动", verified == BASE_VERIFIED)
    check("无 overrides", not overrides)


def case_cross_conflict():
    print("case: 名与包名双向交叉冲突 -> 一并覆盖、两条审计")
    (results, verified, seed_ops, rejected, overrides), _ = run_case(
        [{"name": "己应用", "bundle": "com.g.two", "list": "black"}],
        {"己应用": [{"pkg": "com.g.two"}]})
    r = by_name(results, "己应用")
    check("outcome=overridden", r["outcome"] == "overridden", str(r))
    check("己应用换到 com.g.two",
          verified["己应用"]["bundle"] == "com.g.two")
    check("庚应用条目让位删除", "庚应用" not in verified)
    check("seed_ops 删庚应用", ("remove", "exclude", "庚应用") in seed_ops,
          str(seed_ops))
    check("审计两条", len(overrides) == 2, str(overrides))


def case_invalid_item():
    print("case: 非法条目 -> rejected/error")
    (results, verified, seed_ops, rejected, overrides), _ = run_case(
        [{"name": "", "bundle": "com.x.app", "list": "black"},
         {"name": "某应用", "bundle": "com.x.app", "list": "purple"}], {})
    check("两条均 rejected/error",
          all(r["outcome"] == "rejected" and r.get("stage") == "error"
              for r in results), str(results))
    check("无 conflict-existing 结局再出现",
          all(r.get("stage") != "conflict-existing" for r in results))


def case_remove_seed_file():
    print("case: remove_seed 文件行为（保注释、删一行、缺失不动）")
    root = _TMPDIR / "fakeroot"
    (root / "seeds").mkdir(parents=True, exist_ok=True)
    content = "# 注释头\n甲应用\n# 中间注释\n乙应用\n丙应用\n"
    (root / "seeds" / "exclude.txt").write_text(content, encoding="utf-8")
    old_root = intake.ROOT
    intake.ROOT = root
    try:
        check("删除返回 True", intake.remove_seed("exclude", "乙应用") is True)
        after = (root / "seeds" / "exclude.txt").read_text(encoding="utf-8")
        check("注释与余行原样保留",
              after == "# 注释头\n甲应用\n# 中间注释\n丙应用\n", repr(after))
        check("删不存在的名返回 False 且不动文件",
              intake.remove_seed("exclude", "不存在") is False
              and (root / "seeds" / "exclude.txt").read_text(
                  encoding="utf-8") == after)
    finally:
        intake.ROOT = old_root


def case_append_overrides_file():
    print("case: append_overrides 建档 + 追加")
    opath = _TMPDIR / "overrides.json"
    old = intake.OVERRIDES_PATH
    intake.OVERRIDES_PATH = opath
    try:
        rec = {"date": intake.TODAY, "name": "甲应用",
               "oldBundle": "com.a.old", "newBundle": "com.a.new",
               "oldList": "exclude", "newList": "exclude",
               "origin": "app-submit"}
        check("首次建档", intake.append_overrides([rec]) is True)
        check("二次追加", intake.append_overrides([rec]) is True)
        data = json.loads(opath.read_text(encoding="utf-8"))
        check("共两条", data == [rec, rec], str(data))
        check("空记录不动文件", intake.append_overrides([]) is False)
    finally:
        intake.OVERRIDES_PATH = old


def main():
    for fn in (case_accepted, case_override_bundle, case_override_rename,
               case_override_category, case_already_verified,
               case_gate_rejects_fake_bundle, case_gate_rejects_name_mismatch,
               case_cross_conflict, case_invalid_item,
               case_remove_seed_file, case_append_overrides_file):
        fn()
    print("=== intake self-test ===")
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} 项: {FAILURES}")
        return 1
    print("all passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""App 提交收件处理脚本 —— 仅 Python 标准库。

用途：每日（或手动）拉取提交 Worker 上的待处理应用提交，逐条用与
collect.py 完全相同的核实逻辑（vcck 精确名命中 + appinfo 双向确认）
过闸，过闸的写入 data/verified.json 并追加种子、重生成 dist，
未过闸的记录进 data/submissions-check.json，最后向 Worker ack
已处理项，并（默认）提交推送。

核实逻辑复用同目录 collect.py（import collect）：HmClient /
resolve_name / pick_main_bundle / read_seeds / norm 都在里面；
collect.py 有 __main__ 守卫，import 无副作用（已确认）。

过闸定义（本脚本处理的是带自称包名的提交）：
    提交 bundle == pick_main_bundle(rec["confirmed"])
即本体包名——confirmed 里排除元服务变体（com.atomicservice.*）
后的第一条。提交包名不是本体（哪怕在 confirmed 里）一律不过闸。

冲突口径（2026-11-12 数据集所有者裁决）：任何会改动既有数据的
处理都先过闸，闸口不放水；过闸后若与既有条目冲突（同名不同包名 /
同包名不同名 / 同名同包名但分类不同），不再拒绝，**以提交为准
覆盖**，每次覆盖记入 data/overrides.json（append-only）留痕。
与既有条目完全一致（同名同包名同分类）的为 already-verified，
不查接口、不改数据。

收件端约定：
  - Worker 地址与管理口令优先读环境变量 WORKER_URL / PERAPP_ADMIN_TOKEN
    （GitHub Actions 模式由仓库 Secrets 注入）；无 env 时回落本地
    ~/workspace/cf-perapp-submit/secrets.local.txt
    （键 WORKER_URL=、ADMIN_TOKEN=）；--base-url 可覆盖地址。
  - GET  {WORKER_URL}/pending   头 X-Admin-Token: <ADMIN_TOKEN>
        -> {ok: true, items: [{bundle, name, list, firstSeen, lastSeen, count}]}
  - POST {WORKER_URL}/ack       头同上，体 {bundles: [...]} 删除已处理项。
  - list 取值 "white" | "black"，映射到数据集分类：
        white -> include，black -> exclude。

用法：
    python3 scripts/intake.py [--no-commit] [--base-url URL]
"""

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

# 让 `import collect` 无论从哪个 cwd 启动都能找到同目录的 collect.py
sys.path.insert(0, str(Path(__file__).resolve().parent))

import collect  # noqa: E402  （复用 HmClient/resolve_name/pick_main_bundle/read_seeds/norm）
from collect import HmClient, norm, pick_main_bundle, read_seeds, resolve_name  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SECRETS_PATH = Path.home() / "workspace" / "cf-perapp-submit" / "secrets.local.txt"
TODAY = date.today().isoformat()

VERIFIED_PATH = ROOT / "data" / "verified.json"
SUBMISSIONS_CHECK_PATH = ROOT / "data" / "submissions-check.json"
OVERRIDES_PATH = ROOT / "data" / "overrides.json"
DIST_PATH = ROOT / "dist" / "perapp-lists.json"

LIST_TO_CATEGORY = {"white": "include", "black": "exclude"}
SOURCE_TAG = "app-submit+vcck-search+ag-appinfo"

# 提交时 git add 的文件（相对 ROOT）
COMMIT_PATHS = [
    "data/verified.json",
    "data/submissions-check.json",
    "data/overrides.json",
    "dist/perapp-lists.json",
    "seeds/include.txt",
    "seeds/exclude.txt",
]


class IntakeError(Exception):
    """拉取/配置类致命错误：非零退出并打印原因。"""


# ---------------------------------------------------------------- 配置/收件端
def load_secrets(path=SECRETS_PATH):
    """读 secrets.local.txt，解析 KEY=VALUE（忽略空行与 # 注释）。"""
    p = Path(path)
    if not p.exists():
        raise IntakeError(f"secrets file not found: {p}")
    out = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def get_config(base_url_override=None, secrets_path=SECRETS_PATH):
    """返回 (base_url, admin_token)。

    地址优先级：--base-url > env WORKER_URL > secrets 文件 WORKER_URL；
    口令优先级：env PERAPP_ADMIN_TOKEN > secrets 文件 ADMIN_TOKEN。
    GitHub Actions 模式下 secrets 文件不存在，只走 env；本机模式回落文件。
    """
    secrets = {}
    if Path(secrets_path).exists():
        secrets = load_secrets(secrets_path)
    base = (base_url_override or os.environ.get("WORKER_URL")
            or secrets.get("WORKER_URL") or "").rstrip("/")
    token = os.environ.get("PERAPP_ADMIN_TOKEN") or secrets.get("ADMIN_TOKEN") or ""
    if not base:
        raise IntakeError(
            "WORKER_URL empty or missing (no --base-url, no env, "
            "no value in secrets file)")
    if not token:
        raise IntakeError(
            "admin token missing (no PERAPP_ADMIN_TOKEN env, "
            "no ADMIN_TOKEN in secrets file)")
    return base, token


def _worker_request(base, token, method, path, payload=None, timeout=30):
    url = base + path
    headers = {"X-Admin-Token": token, "Accept": "application/json",
               # The worker zone's Browser Integrity Check rejects the default
               # Python-urllib signature (CF error 1010); send a browser UA.
               "User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
                             "(KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36"}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:500]
        except Exception:
            pass
        if e.code in (401, 403):
            raise IntakeError(
                f"{method} {path} failed: HTTP {e.code} "
                f"(admin token rejected) {body}".strip())
        raise IntakeError(f"{method} {path} failed: HTTP {e.code} {body}".strip())
    except Exception as e:
        raise IntakeError(f"{method} {path} failed: {e}")
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        raise IntakeError(f"{method} {path} failed: non-JSON response: {raw[:200]!r}")


def fetch_pending(base, token):
    """GET /pending -> items 列表。失败/口令错抛 IntakeError。"""
    data = _worker_request(base, token, "GET", "/pending")
    if isinstance(data, dict) and data.get("ok") is False:
        raise IntakeError(f"GET /pending failed: worker returned ok=false: {data}")
    items = data.get("items") if isinstance(data, dict) else None
    if items is None:
        raise IntakeError(f"GET /pending failed: unexpected response shape: {data!r}")
    if not isinstance(items, list):
        raise IntakeError(f"GET /pending failed: items is not a list: {items!r}")
    return items


def ack_processed(base, token, bundles):
    """POST /ack {bundles:[...]} 删除已处理项。失败抛 IntakeError。"""
    if not bundles:
        return {}
    data = _worker_request(base, token, "POST", "/ack", {"bundles": list(bundles)})
    if isinstance(data, dict) and data.get("ok") is False:
        raise IntakeError(f"POST /ack failed: worker returned ok=false: {data}")
    return data


# ---------------------------------------------------------------- 过闸与 stage 判定
def find_confirmation(rec, bundle):
    """confirmations 中 bundle 对应的那条确认记录，没有则 None。"""
    for c in (rec or {}).get("confirmations") or []:
        if c.get("bundle") == bundle:
            return c
    return None


def gate_passes(rec, bundle):
    """过闸：提交 bundle == pick_main_bundle(rec["confirmed"])。"""
    if not bundle:
        return False
    return pick_main_bundle((rec or {}).get("confirmed") or []) == bundle


def rejection_stage(rec, name, bundle):
    """未过闸时按证据判定 stage（判定顺序即证据强度顺序）。

    - bundle 不在 rec["phoneCandidates"]        -> vcck-no-exact-phone-hit
    - 在候选中但 confirmation.byBundlePkg != bundle -> appinfo-by-bundle-mismatch
    - byIdPkg != bundle                          -> appinfo-by-id-mismatch
    - byBundleName 与提交 name 规范化后不等       -> name-mismatch
    - bundle 在 confirmed 里但不是 pick_main_bundle -> not-main-bundle
    （confirmation 缺失时 byBundlePkg 视为 None，归 appinfo-by-bundle-mismatch；
      上述证据全对上却仍不过闸，只剩「在 confirmed 里但非本体」一种可能，
      兜底同样归 not-main-bundle。）
    """
    rec = rec or {}
    if bundle not in (rec.get("phoneCandidates") or []):
        return "vcck-no-exact-phone-hit"
    conf = find_confirmation(rec, bundle)
    if conf is None or conf.get("byBundlePkg") != bundle:
        return "appinfo-by-bundle-mismatch"
    if conf.get("byIdPkg") != bundle:
        return "appinfo-by-id-mismatch"
    if norm(conf.get("byBundleName")) != norm(name):
        return "name-mismatch"
    # byBundle/byId/返回名都对上：若在 confirmed 里，必是输给了本体排序；
    # 不在 confirmed 里也无其他证据可分，一并归此类。
    return "not-main-bundle"


def category_for(list_value):
    """提交 list ("white"|"black") -> 数据集分类 ("include"|"exclude")。"""
    return LIST_TO_CATEGORY.get(list_value)


# ---------------------------------------------------------------- 文件读写
def _dump_json(obj):
    return json.dumps(obj, ensure_ascii=False, indent=1) + "\n"


def append_seed(category, name):
    """把 name 追加进 seeds/<category>.txt 末尾（已在则不重复）。

    seeds 文件是注释头 + 一行一名；read_seeds 已做去重判定依据
    （忽略注释行与空行）。追加前保证文件以换行结尾。
    """
    if name in read_seeds(category):
        return False
    path = ROOT / "seeds" / f"{category}.txt"
    text = path.read_text(encoding="utf-8")
    if text and not text.endswith("\n"):
        text += "\n"
    path.write_text(text + name + "\n", encoding="utf-8")
    return True


def remove_seed(category, name):
    """把 name 从 seeds/<category>.txt 删除（覆盖改名/移分类用）。

    只删内容行（去空白后与 name 全等且非注释行），注释与其余行原样
    保留、顺序不变。不在文件中则不动文件、返回 False。
    """
    path = ROOT / "seeds" / f"{category}.txt"
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    kept, removed = [], False
    for line in lines:
        stripped = line.strip()
        if (not removed and stripped == name
                and not stripped.startswith("#")):
            removed = True
            continue
        kept.append(line)
    if not removed:
        return False
    path.write_text("".join(kept), encoding="utf-8")
    return True


def regenerate_dist(verified):
    """按 collect.py main() 第 3 步同法重生成 dist/perapp-lists.json。"""
    seeds = {cat: read_seeds(cat) for cat in ("include", "exclude")}
    dist = {}
    for cat in ("include", "exclude"):
        dist[cat] = [{"name": s, "bundle": verified[s]["bundle"],
                      "verified": True}
                     for s in seeds[cat] if s in verified]
    DIST_PATH.write_text(_dump_json(dist), encoding="utf-8")
    return dist


def append_submissions_check(records):
    """把本轮 rejected 记录 append 进 data/submissions-check.json。

    顶层 JSON 数组；文件不存在则新建，已存在则读出 append 后写回。
    保持 ensure_ascii=False、indent=1 的仓内风格。无记录时不动文件。
    """
    if not records:
        return False
    existing = []
    if SUBMISSIONS_CHECK_PATH.exists():
        raw = SUBMISSIONS_CHECK_PATH.read_text(encoding="utf-8").strip()
        if raw:
            parsed = json.loads(raw)
            if not isinstance(parsed, list):
                raise IntakeError(
                    f"{SUBMISSIONS_CHECK_PATH} exists but is not a JSON array")
            existing = parsed
    existing.extend(records)
    SUBMISSIONS_CHECK_PATH.write_text(_dump_json(existing), encoding="utf-8")
    return True


def append_overrides(records):
    """把本轮覆盖记录 append 进 data/overrides.json（append-only 审计）。

    顶层 JSON 数组；文件不存在则新建。记录形如
    {date, name, oldBundle, newBundle, oldList, newList, origin}，
    改名时另带 oldName。无记录时不动文件。
    """
    if not records:
        return False
    existing = []
    if OVERRIDES_PATH.exists():
        raw = OVERRIDES_PATH.read_text(encoding="utf-8").strip()
        if raw:
            parsed = json.loads(raw)
            if not isinstance(parsed, list):
                raise IntakeError(
                    f"{OVERRIDES_PATH} exists but is not a JSON array")
            existing = parsed
    existing.extend(records)
    OVERRIDES_PATH.write_text(_dump_json(existing), encoding="utf-8")
    return True


# ---------------------------------------------------------------- git 提交
def _git(*args):
    return subprocess.run(["git", "-C", str(ROOT), *args],
                          capture_output=True, text=True)


def maybe_commit(n_accepted, n_overridden, n_rejected, no_commit):
    """有 accepted/overridden 或 submissions-check 有新增、且工作区
    确有变化时提交推送。

    返回状态字符串：skipped-no-commit / no-changes / committed /
    commit-failed:<原因>。只 add COMMIT_PATHS 里的文件。
    """
    if no_commit:
        return "skipped-no-commit"
    if n_accepted <= 0 and n_overridden <= 0 and n_rejected <= 0:
        return "no-changes"
    existing = [p for p in COMMIT_PATHS if (ROOT / p).exists()]
    if not existing:
        return "no-changes"
    status = _git("status", "--porcelain", "--", *existing)
    if status.returncode != 0:
        return f"commit-failed: git status: {status.stderr.strip()}"
    if not status.stdout.strip():
        return "no-changes"
    add = _git("add", "--", *existing)
    if add.returncode != 0:
        return f"commit-failed: git add: {add.stderr.strip()}"
    # add 后再确认暂存区确有变化（porcelain 对已 add 的文件显示为已暂存）
    staged = _git("diff", "--cached", "--name-only", "--", *existing)
    if staged.returncode != 0 or not staged.stdout.strip():
        return "no-changes"
    if n_accepted > 0 or n_overridden > 0:
        message = (f"feat: +{n_accepted} verified, {n_overridden} overridden "
                   f"from app submissions")
    else:
        message = f"chore: record {n_rejected} rejected app submissions"
    commit = _git("commit", "-m", message)
    if commit.returncode != 0:
        return f"commit-failed: git commit: {(commit.stderr or commit.stdout).strip()}"
    push = _git("push", "origin", "main")
    if push.returncode != 0:
        return f"commit-failed: git push: {(push.stderr or push.stdout).strip()}"
    return "committed"


# ---------------------------------------------------------------- 主流程
def _build_entry(rec, name, bundle):
    """按确认记录构造 verified 条目（accepted 与覆盖共用）。"""
    conf = find_confirmation(rec, bundle) or {}
    entry = {
        "bundle": bundle,
        "fullName": conf.get("byBundleName") or name,
        "source": SOURCE_TAG,
        "collectedAt": TODAY,
    }
    if conf.get("developerName"):
        entry["developer"] = conf["developerName"]
    return entry


def process_items(items, hm=None):
    """逐条处理 pending 项。

    返回 (results, verified, seed_ops, rejected_records, override_records)：
      results: [{name, bundle, list, category, outcome, stage?, detail?}]
        outcome ∈ accepted / overridden / already-verified / rejected
      verified: 更新后的 verified dict
      seed_ops: [("append"|"remove", category, name), ...]，由 main 按序
        落盘（append_seed/remove_seed），与内存里的分类状态保持一致
      rejected_records: 待写入 submissions-check 的记录
      override_records: 待写入 overrides.json 的审计记录
    单条抛异常记 rejected/stage="error" 并继续其余条。

    处理顺序（2026-11-12 裁决口径）：与既有条目完全一致（同名同包名
    同分类）直接 already-verified；其余一律先过三向确认闸，过不了闸
    按证据 stage 拒绝；过闸后无冲突为新入选 accepted，有冲突以提交
    为准覆盖 overridden 并留审计记录。
    """
    if hm is None:
        hm = HmClient()
    verified = json.loads(VERIFIED_PATH.read_text(encoding="utf-8"))
    # bundle -> 当前持有它的应用名（同轮变更随之更新）
    bundle_owner = {}
    for nm, v in verified.items():
        if isinstance(v, dict) and v.get("bundle"):
            bundle_owner[v["bundle"]] = nm
    # 应用名当前的分类（以 seeds 成员关系为准），随本轮变更同步维护
    seeds_map = {cat: list(read_seeds(cat)) for cat in ("include", "exclude")}

    def _category_of(nm):
        for cat in ("include", "exclude"):
            if nm in seeds_map[cat]:
                return cat
        return None

    results = []
    seed_ops = []
    rejected_records = []
    override_records = []

    def _seed_append(cat, nm):
        if nm not in seeds_map[cat]:
            seeds_map[cat].append(nm)
            seed_ops.append(("append", cat, nm))

    def _seed_remove(cat, nm):
        if cat and nm in seeds_map[cat]:
            seeds_map[cat].remove(nm)
            seed_ops.append(("remove", cat, nm))

    for item in items:
        bundle = (item or {}).get("bundle")
        name = (item or {}).get("name")
        list_value = (item or {}).get("list")
        category = category_for(list_value)
        result = {"name": name, "bundle": bundle, "list": list_value,
                  "category": category}

        def _reject(stage):
            result["outcome"] = "rejected"
            result["stage"] = stage
            rejected_records.append({
                "name": name, "bundle": bundle, "list": list_value,
                "category": category, "stage": stage, "date": TODAY})

        try:
            if not bundle or not name or category is None:
                _reject("error")
                results.append(result)
                continue

            name_entry = verified.get(name)
            owner = bundle_owner.get(bundle)  # 持有该 bundle 的名字（可能为 None）
            cur_cat = _category_of(name) if name_entry else None

            if (name_entry is not None
                    and name_entry.get("bundle") == bundle
                    and owner == name
                    and cur_cat == category):
                # 与既有条目完全一致：不查接口、不改数据
                result["outcome"] = "already-verified"
                results.append(result)
                continue

            # 任何数据改动先过闸，闸口不放水
            rec = resolve_name(hm, name)
            if not gate_passes(rec, bundle):
                _reject(rejection_stage(rec, name, bundle))
                results.append(result)
                continue

            if name_entry is None and owner is None:
                # 无冲突：新入选
                verified[name] = _build_entry(rec, name, bundle)
                bundle_owner[bundle] = name
                _seed_append(category, name)
                result["outcome"] = "accepted"
                results.append(result)
                continue

            # 有冲突：以提交为准覆盖，逐个被顶替的既有条目留审计
            result["outcome"] = "overridden"
            details = []

            if owner is not None and owner != name:
                # 同包名不同名：旧名条目让位（改名）
                owner_cat = _category_of(owner)
                del verified[owner]
                _seed_remove(owner_cat, owner)
                override_records.append({
                    "date": TODAY, "name": name, "oldName": owner,
                    "oldBundle": bundle, "newBundle": bundle,
                    "oldList": owner_cat, "newList": category,
                    "origin": "app-submit"})
                details.append(f"oldName={owner}")

            if name_entry is not None and name_entry.get("bundle") != bundle:
                # 同名不同包名：换包名（分类不同则一并移分类）
                old_bundle = name_entry.get("bundle")
                verified[name] = _build_entry(rec, name, bundle)
                if bundle_owner.get(old_bundle) == name:
                    del bundle_owner[old_bundle]
                if cur_cat != category:
                    _seed_remove(cur_cat, name)
                    _seed_append(category, name)
                override_records.append({
                    "date": TODAY, "name": name,
                    "oldBundle": old_bundle, "newBundle": bundle,
                    "oldList": cur_cat, "newList": category,
                    "origin": "app-submit"})
                details.append(f"oldBundle={old_bundle}")
            elif name_entry is not None:
                # 同名同包名、仅分类不同：移分类，条目本身不动
                if cur_cat != category:
                    _seed_remove(cur_cat, name)
                    _seed_append(category, name)
                    override_records.append({
                        "date": TODAY, "name": name,
                        "oldBundle": bundle, "newBundle": bundle,
                        "oldList": cur_cat, "newList": category,
                        "origin": "app-submit"})
                    details.append(f"oldList={cur_cat}")
            else:
                # 纯改名（名是新的、包名被旧名占用，已在上面让位）：落新条目
                verified[name] = _build_entry(rec, name, bundle)
                _seed_append(category, name)

            bundle_owner[bundle] = name
            if details:
                result["detail"] = " ".join(details)
        except Exception as e:  # 单条失败不中断全轮
            print(f"  [warn] intake failed for {name} ({bundle}): {e}",
                  file=sys.stderr)
            if result.get("outcome") is None:
                _reject("error")
        results.append(result)

    return results, verified, seed_ops, rejected_records, override_records


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Process pending app submissions into harmonyos-app-lists.")
    parser.add_argument("--no-commit", action="store_true",
                        help="only modify files, do not git commit/push")
    parser.add_argument("--base-url", default=None,
                        help="override WORKER_URL from secrets file")
    args = parser.parse_args(argv)

    try:
        base, token = get_config(args.base_url)
    except IntakeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    try:
        items = fetch_pending(base, token)
    except IntakeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    if not items:
        print("no pending")
        return 0

    print(f"pending: {len(items)}")

    (results, verified, seed_ops,
     rejected_records, override_records) = process_items(items)

    n_accepted = sum(1 for r in results if r["outcome"] == "accepted")
    n_overridden = sum(1 for r in results if r["outcome"] == "overridden")
    n_rejected = sum(1 for r in results if r["outcome"] == "rejected")
    n_already = sum(1 for r in results if r["outcome"] == "already-verified")

    # 文件落盘：accepted/overridden -> verified + seeds + dist + overrides；
    # rejected -> submissions-check
    if n_accepted > 0 or n_overridden > 0:
        VERIFIED_PATH.write_text(_dump_json(verified), encoding="utf-8")
        for op, category, name in seed_ops:
            if op == "append":
                append_seed(category, name)
            else:
                remove_seed(category, name)
        regenerate_dist(verified)
    if override_records:
        try:
            append_overrides(override_records)
        except IntakeError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
    check_changed = False
    if rejected_records:
        try:
            check_changed = append_submissions_check(rejected_records)
        except IntakeError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1

    # 逐条结果
    for r in results:
        line = f"{r['outcome']}: {r['name']} ({r['bundle']}) list={r['list']}"
        if r.get("stage"):
            line += f" stage={r['stage']}"
        if r.get("detail"):
            line += f" {r['detail']}"
        print(line)

    # 全部条目处理完 -> ack 全部 bundle 清除
    ack_error = None
    all_bundles = [r["bundle"] for r in results if r.get("bundle")]
    if all_bundles:
        try:
            ack_processed(base, token, all_bundles)
            print(f"ack: {len(all_bundles)} bundles cleared")
        except IntakeError as e:
            ack_error = str(e)
            print(f"error: {ack_error}", file=sys.stderr)

    # 提交（默认）：有 accepted/overridden 或 submissions-check 有新增
    # 且确有变化才提交
    commit_status = maybe_commit(
        n_accepted, n_overridden,
        len(rejected_records) if check_changed else 0,
        no_commit=args.no_commit)
    print(f"commit: {commit_status}")

    print("=== summary ===")
    print(f"accepted={n_accepted} overridden={n_overridden} "
          f"rejected={n_rejected} already-verified={n_already} "
          f"total={len(results)}")

    if ack_error:
        return 1
    if commit_status.startswith("commit-failed"):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

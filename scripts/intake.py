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

收件端约定：
  - Worker 地址与管理口令读 ~/workspace/cf-perapp-submit/secrets.local.txt
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
DIST_PATH = ROOT / "dist" / "perapp-lists.json"

LIST_TO_CATEGORY = {"white": "include", "black": "exclude"}
SOURCE_TAG = "app-submit+vcck-search+ag-appinfo"

# 提交时 git add 的文件（相对 ROOT）
COMMIT_PATHS = [
    "data/verified.json",
    "data/submissions-check.json",
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
    """返回 (base_url, admin_token)；--base-url 覆盖 WORKER_URL。"""
    secrets = load_secrets(secrets_path)
    base = (base_url_override or secrets.get("WORKER_URL") or "").rstrip("/")
    token = secrets.get("ADMIN_TOKEN") or ""
    if not base:
        raise IntakeError(
            "WORKER_URL empty or missing (secrets file has no value "
            "and no --base-url given)")
    if not token:
        raise IntakeError("ADMIN_TOKEN missing in secrets file")
    return base, token


def _worker_request(base, token, method, path, payload=None, timeout=30):
    url = base + path
    headers = {"X-Admin-Token": token, "Accept": "application/json"}
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


# ---------------------------------------------------------------- git 提交
def _git(*args):
    return subprocess.run(["git", "-C", str(ROOT), *args],
                          capture_output=True, text=True)


def maybe_commit(n_accepted, n_rejected, no_commit):
    """有 accepted 或 submissions-check 有新增、且工作区确有变化时提交推送。

    返回状态字符串：skipped-no-commit / no-changes / committed /
    commit-failed:<原因>。只 add COMMIT_PATHS 里的文件。
    """
    if no_commit:
        return "skipped-no-commit"
    if n_accepted <= 0 and n_rejected <= 0:
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
    if n_accepted > 0:
        message = f"feat: +{n_accepted} verified from app submissions"
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
def process_items(items, hm=None):
    """逐条处理 pending 项。

    返回 (results, verified, seeds_to_append, rejected_records)：
      results: [{name, bundle, list, category, outcome, stage?}, ...]
      verified: 更新后的 verified dict（仅 accepted 时有新增）
      seeds_to_append: [(category, name), ...] 待追加种子
      rejected_records: 待写入 submissions-check 的记录
    单条抛异常记 rejected/stage="error" 并继续其余条。
    """
    if hm is None:
        hm = HmClient()
    verified = json.loads(VERIFIED_PATH.read_text(encoding="utf-8"))
    verified_bundles = {v.get("bundle") for v in verified.values()
                        if isinstance(v, dict)}

    results = []
    seeds_to_append = []
    rejected_records = []

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
            elif bundle in verified_bundles:
                result["outcome"] = "already-verified"
            elif name in verified and verified[name].get("bundle") != bundle:
                # verified 绝不静默覆盖：同名不同包名只记录冲突
                _reject("conflict-existing")
            else:
                rec = resolve_name(hm, name)
                if gate_passes(rec, bundle):
                    conf = find_confirmation(rec, bundle) or {}
                    entry = {
                        "bundle": bundle,
                        "fullName": conf.get("byBundleName") or name,
                        "source": SOURCE_TAG,
                        "collectedAt": TODAY,
                    }
                    if conf.get("developerName"):
                        entry["developer"] = conf["developerName"]
                    verified[name] = entry
                    verified_bundles.add(bundle)
                    seeds_to_append.append((category, name))
                    result["outcome"] = "accepted"
                else:
                    _reject(rejection_stage(rec, name, bundle))
        except Exception as e:  # 单条失败不中断全轮
            print(f"  [warn] intake failed for {name} ({bundle}): {e}",
                  file=sys.stderr)
            if result.get("outcome") is None:
                _reject("error")
        results.append(result)

    return results, verified, seeds_to_append, rejected_records


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

    results, verified, seeds_to_append, rejected_records = process_items(items)

    n_accepted = sum(1 for r in results if r["outcome"] == "accepted")
    n_rejected = sum(1 for r in results if r["outcome"] == "rejected")
    n_already = sum(1 for r in results if r["outcome"] == "already-verified")

    # 文件落盘：accepted -> verified + seeds + dist；rejected -> submissions-check
    if n_accepted > 0:
        VERIFIED_PATH.write_text(_dump_json(verified), encoding="utf-8")
        for category, name in seeds_to_append:
            append_seed(category, name)
        regenerate_dist(verified)
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

    # 提交（默认）：有 accepted 或 submissions-check 有新增且确有变化才提交
    commit_status = maybe_commit(
        n_accepted, len(rejected_records) if check_changed else 0,
        no_commit=args.no_commit)
    print(f"commit: {commit_status}")

    print("=== summary ===")
    print(f"accepted={n_accepted} rejected={n_rejected} "
          f"already-verified={n_already} total={len(results)}")

    if ack_error:
        return 1
    if commit_status.startswith("commit-failed"):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""启用排期版本管理、更正影响面计算与原子发布切换。"""

import hashlib
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.domain.rollout import normalize_schedule
from src.models.jobs import Run
from src.models.rollout import ActivationVersion


def checksum(entries):
    normalized = normalize_schedule(entries)
    payload = [{"market_id": market, "activate_date": day} for market, day in normalized.items()]
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(raw.encode()).hexdigest(), payload


def create_version(db: Session, dataset_id, name, entries, author, note=None):
    """在单个事务内写入新版本并原子切换生效标记；同内容重复提交返回既有版本。"""
    signature, payload = checksum(entries)
    duplicate = db.scalar(select(ActivationVersion).where(
        ActivationVersion.dataset_id == dataset_id, ActivationVersion.checksum == signature))
    if duplicate:
        return duplicate, False

    previous = db.scalar(select(ActivationVersion).where(
        ActivationVersion.dataset_id == dataset_id, ActivationVersion.is_active.is_(True)
    ).order_by(ActivationVersion.version.desc()).limit(1))
    next_version = (previous.version + 1) if previous else 1
    version = ActivationVersion(
        dataset_id=dataset_id, version=next_version, name=name, entries=payload,
        checksum=signature, is_active=True, supersedes_id=previous.id if previous else None,
        note=(note or None), author=author)
    db.add(version)
    db.flush()
    if previous:
        previous.is_active = False
    return version, True


def _entry_map(version: ActivationVersion):
    return {item["market_id"]: item["activate_date"] for item in version.entries}


def affected_markets(old: ActivationVersion, new: ActivationVersion):
    """排期更正中启用日发生变化（含新增、移除）的市场。"""
    old_map, new_map = _entry_map(old), _entry_map(new)
    markets = set(old_map) | set(new_map)
    return sorted(market for market in markets if old_map.get(market) != new_map.get(market))


def impacted_runs(db: Session, new: ActivationVersion):
    """受新版排期影响的历史运行（覆盖连续更正、两次更正同时提交的情形）。

    扫描所有基于更旧版本的运行，逐一比较该运行所用排期与新版：启用日发生变化
    的市场一旦出现在运行的试点或对照集合中，该运行就需重算——含原来拿该市场
    作对照的批次。
    """
    new_map = _entry_map(new)
    runs = list(db.scalars(select(Run).where(
        Run.activation_version_id.is_not(None),
        Run.activation_version_id != new.id).order_by(Run.id)))
    impacted = []
    for run in runs:
        used_version = db.get(ActivationVersion, run.activation_version_id)
        if used_version is None or used_version.dataset_id != new.dataset_id:
            continue
        used_map = _entry_map(used_version)
        changed = {market for market in set(new_map) | set(used_map)
                   if used_map.get(market) != new_map.get(market)}
        # 运行尚未完成（无结果）时，保守按该版排期全量市场判定。
        if changed and _run_uses_markets(run, changed, set(used_map)):
            impacted.append(run)
    return impacted


def _run_uses_markets(run: Run, markets, fallback_markets):
    result = run.result or {}
    used = set()
    for batch in result.get("batches", []):
        used.update(batch.get("treated_markets", []))
        used.update(batch.get("control_candidates", []))
        used.update(batch.get("control_kept", []))
    for item in result.get("schedule", []):
        used.add(item.get("market_id"))
    if not used:
        used = set(fallback_markets)
    return bool(used & markets)


def recompute_plan(db: Session, new: ActivationVersion):
    """生成重算清单：每个受影响的「线上发布组」只取最新一代运行重算。

    连续更正（含两次更正同时提交）时，旧代运行已被同组新代取代的，不再重复重算。
    """
    impacted = impacted_runs(db, new)
    latest_by_group = {}
    for run in impacted:
        group = run.publish_group or f'run-{run.id}'
        if group not in latest_by_group or run.id > latest_by_group[group].id:
            latest_by_group[group] = run
    plan = []
    for run in sorted(latest_by_group.values(), key=lambda item: item.id):
        group = run.publish_group or f'run-{run.id}'
        plan.append({
            "source_run_id": run.id,
            "dataset_id": run.dataset_id,
            "name": f"{run.name}（排期 v{new.version} 重算）",
            "params": dict(run.params),
            "publish_group": group,
            "author": run.author,
        })
    return plan


def switch_published(db: Session, new_runs, publish_group):
    """页面与下载一次性切换：全部新运行成功后，同组旧发布下线、新版同时上线。

    以本次传入的运行 id 集合区分新一代与历史代；任一运行未成功则不切换，
    调用方应保留旧版。返回是否完成切换。
    """
    from src.constants.enums import Status
    from src.utils.time import now
    if not new_runs or any(run.status != Status.COMPLETE for run in new_runs):
        return False
    new_ids = {run.id for run in new_runs}
    stamp = now()
    for run in db.scalars(select(Run).where(Run.publish_group == publish_group)):
        if run.id in new_ids:
            run.published = True
            run.published_at = stamp
        else:
            # 同组的历史代运行整体下线，记录与导出仍保留可查。
            run.published = False
    return True

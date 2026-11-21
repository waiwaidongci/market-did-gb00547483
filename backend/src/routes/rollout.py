from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session
from src.config.database import connection
from src.constants.enums import Role
from src.controllers.auth import allow, current
from src.models.identity import User
from src.models.jobs import Run
from src.models.rollout import ActivationVersion
from src.models.source import Dataset
from src.schemas.requests import RecalculateRequest, RolloutRunRequest, VersionRequest
from src.services import rollout as rollout_service
from src.services.audit import record
from src.services.validation import normalize_rollout_params
from src.utils import views
from src.utils.time import iso
from src.utils.wakeup import wake

router = APIRouter()


def _version_view(db: Session, version: ActivationVersion):
    superseded = version.supersedes_id and db.get(ActivationVersion, version.supersedes_id)
    affected = (rollout_service.affected_markets(superseded, version)
                if superseded else [])
    return {
        'id': version.id, 'dataset_id': version.dataset_id, 'version': version.version,
        'name': version.name, 'entries': version.entries, 'checksum': version.checksum,
        'is_active': version.is_active, 'supersedes_id': version.supersedes_id,
        'note': version.note, 'author': version.author, 'created_at': iso(version.created_at),
        'affected_markets': affected,
    }


@router.get('/activation-versions')
def versions(dataset_id: int, db: Session = Depends(connection), actor: User = Depends(current)):
    if not db.get(Dataset, dataset_id):
        raise HTTPException(404, '找不到指定的数据批次')
    items = db.scalars(select(ActivationVersion).where(
        ActivationVersion.dataset_id == dataset_id).order_by(ActivationVersion.version.desc()))
    return [_version_view(db, item) for item in items]


@router.post('/activation-versions', status_code=201)
def submit_version(body: VersionRequest, db: Session = Depends(connection),
                   actor: User = Depends(allow(Role.ADMIN, Role.ANALYST))):
    if not db.get(Dataset, body.dataset_id):
        raise HTTPException(404, '找不到指定的数据批次')
    previous = db.scalar(select(ActivationVersion).where(
        ActivationVersion.dataset_id == body.dataset_id,
        ActivationVersion.is_active.is_(True)).order_by(ActivationVersion.version.desc()).limit(1))
    entries = [item.model_dump() for item in body.entries]
    version, created = rollout_service.create_version(
        db, body.dataset_id, body.name, entries, actor.username, body.note)
    if created:
        record(db, actor.username, 'activation.version_created', version.id,
               version=version.version, supersedes_id=version.supersedes_id,
               affected=rollout_service.affected_markets(previous, version) if previous else [])
        db.commit()
    return {**_version_view(db, version), 'created': created}


@router.get('/activation-versions/{version_id}/impact')
def correction_impact(version_id: int, db: Session = Depends(connection),
                      actor: User = Depends(current)):
    new = db.get(ActivationVersion, version_id)
    if not new:
        raise HTTPException(404, '找不到该排期版本')
    if not new.supersedes_id:
        return {'version_id': version_id, 'affected_markets': [], 'impacted_runs': [], 'plan': []}
    old = db.get(ActivationVersion, new.supersedes_id)
    impacted = rollout_service.impacted_runs(db, new)
    plan = rollout_service.recompute_plan(db, new)
    return {
        'version_id': version_id,
        'affected_markets': rollout_service.affected_markets(old, new),
        'impacted_runs': [views.run(item) for item in impacted],
        'plan': plan,
    }


@router.post('/rollout-runs', status_code=202)
def start_rollout_run(body: RolloutRunRequest, db: Session = Depends(connection),
                      actor: User = Depends(allow(Role.ADMIN, Role.ANALYST))):
    if not db.get(Dataset, body.dataset_id):
        raise HTTPException(404, '找不到指定的数据批次')
    version = db.get(ActivationVersion, body.activation_version_id)
    if not version or version.dataset_id != body.dataset_id:
        raise HTTPException(404, '找不到该数据批次下的排期版本')
    item = Run(name=body.name, dataset_id=body.dataset_id, kind='rollout',
               activation_version_id=version.id,
               params=normalize_rollout_params(body.params),
               publish_group=body.publish_group or None, author=actor.username)
    db.add(item)
    db.flush()
    record(db, actor.username, 'analysis.queued', item.id, dataset_id=item.dataset_id,
           activation_version_id=version.id, publish_group=item.publish_group)
    db.commit()
    wake()
    return views.run(item, detail=True)


@router.post('/recalculations', status_code=202)
def recalculate(body: RecalculateRequest, db: Session = Depends(connection),
                actor: User = Depends(allow(Role.ADMIN, Role.ANALYST))):
    """按新版排期重算所有受影响批次，含原来拿变更市场作对照的批次。

    同一发布组内的重算运行全部成功后才一次性切换页面与下载；任一失败保留旧版。
    """
    new = db.get(ActivationVersion, body.activation_version_id)
    if not new:
        raise HTTPException(404, '找不到该排期版本')
    if not new.supersedes_id:
        raise HTTPException(409, '首个排期版本没有需要更正重算的历史运行')
    plan = rollout_service.recompute_plan(db, new)
    if not plan:
        raise HTTPException(409, '该更正没有影响任何历史运行，无需重算')
    created = []
    for step in plan:
        group = step['publish_group']
        item = Run(name=step['name'], dataset_id=step['dataset_id'], kind='rollout',
                   activation_version_id=new.id, params=step['params'],
                   publish_group=group, author=actor.username)
        db.add(item)
        db.flush()
        created.append(item)
        # 把来源运行并入同一发布组，切换时旧版才能被整组下线（独立运行原本无组）。
        source = db.get(Run, step['source_run_id'])
        if source and source.publish_group != group:
            source.publish_group = group
        record(db, actor.username, 'recalculation.queued', item.id,
               source_run_id=step['source_run_id'], activation_version_id=new.id,
               publish_group=group)
    db.commit()
    wake()
    return {'publish_groups': sorted({step['publish_group'] for step in plan}),
            'run_ids': [item.id for item in created],
            'runs': [views.run(item, detail=True) for item in created]}


@router.get('/published')
def published(dataset_id: int | None = None, db: Session = Depends(connection),
              actor: User = Depends(current)):
    """页面与下载的当前发布视图：只返回已发布运行；旧版未切换前仍是它。"""
    query = select(Run).where(Run.published.is_(True)).order_by(Run.id.desc())
    if dataset_id:
        query = query.where(Run.dataset_id == dataset_id)
    return [views.run(item, detail=True) for item in db.scalars(query.limit(100))]

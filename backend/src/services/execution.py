import logging
from datetime import date, timedelta
from sqlalchemy import and_, or_, select
from src.config.database import Session
from src.constants.enums import Status
from src.domain.analysis import analyze
from src.domain.rollout import analyze_rollout
from src.domain.spec import SPEC
from src.models.jobs import Delivery, Run, Schedule
from src.models.rollout import ActivationVersion
from src.models.source import Dataset
from src.services import rollout as rollout_service
from src.services.audit import record
from src.utils.exports import write_report
from src.utils.time import now

log = logging.getLogger(__name__)


def materialize_due():
    if not SPEC.get('scheduled'):
        return
    stamp = now()
    with Session.begin() as db:
        schedules = db.scalars(select(Schedule).where(Schedule.enabled.is_(True), Schedule.next_at <= stamp)
                                .order_by(Schedule.next_at).limit(20).with_for_update(skip_locked=True))
        for item in schedules:
            due = item.next_at
            dataset_id = item.dataset_id
            params = dict(item.params)
            if item.repeat_daily:
                elapsed_days = (stamp - due).days
                due += timedelta(days=elapsed_days)
                period_key = SPEC['schedule_date_param']
                report_day = date.fromisoformat(params[period_key]) + timedelta(days=elapsed_days)
                params[period_key] = report_day.isoformat()
                item.params = {**params, period_key: (report_day + timedelta(days=1)).isoformat()}
                dataset_id = db.scalar(select(Dataset.id).order_by(Dataset.id.desc()).limit(1)) or dataset_id
                item.next_at = due + timedelta(days=1)
            else:
                item.enabled = False
            db.add(Run(dataset_id=dataset_id, schedule_id=item.id, scheduled_at=due,
                       name=item.name, params=params, author=item.author))


def claim():
    stamp = now()
    with Session.begin() as db:
        query = select(Run).where(or_(Run.status == Status.QUEUED,
                                      and_(Run.status == Status.RUNNING, Run.lease_until < stamp)))
        job = db.scalar(query.order_by(Run.id).limit(1).with_for_update(skip_locked=True))
        if job is None:
            return None
        if job.attempt >= 3:
            job.status = Status.FAILED
            job.error = '后台进程多次中断，请发起新的运行'
            job.finished_at = stamp
            record(db, 'worker', 'analysis.failed', job.id, reason=job.error)
            return None
        job.status = Status.RUNNING
        job.attempt += 1
        job.lease_until = stamp + timedelta(minutes=3)
        records = db.get(Dataset, job.dataset_id).rows
        entries = db.get(ActivationVersion, job.activation_version_id).entries if job.activation_version_id else None
        return job.id, job.attempt, records, job.params, job.activation_version_id, entries


def execute(lease):
    job_id, attempt, rows, params, version_id, schedule_entries = lease
    result = export = failure = None
    try:
        if version_id:
            result = analyze_rollout(rows, params, schedule_entries)
        else:
            result = analyze(rows, params)
        export = write_report(job_id, attempt, result)
    except ValueError as error:
        failure = str(error)[:500]
    except Exception:
        log.exception('Analysis %s failed', job_id)
        failure = '计算失败，请检查字段口径后重试'
    with Session.begin() as db:
        job = db.get(Run, job_id, with_for_update=True)
        if job.attempt != attempt or job.status != Status.RUNNING:
            return
        job.lease_until = None
        job.finished_at = now()
        job.status = Status.FAILED if failure else Status.COMPLETE
        job.result = None if failure else result
        job.export_file = export
        job.error = failure
        record(db, 'worker', f'analysis.{job.status}', job.id, attempt=attempt, reason=failure)
        if not failure:
            _apply_publish(db, job)
        # 统一启用日的历史运行照常可查：保留结果与导出，不参与分批发布切换。
        if job.schedule_id:
            plan = db.get(Schedule, job.schedule_id)
            ready = not failure and result.get('ready', True)
            detail = '已保存 CSV 到站内收件箱' if ready else (failure or result['headline'])
            receipt = Delivery(run_id=job.id, recipient=plan.recipient, state='delivered' if ready else 'blocked', detail=detail[:500])
            db.add(receipt)
            record(db, 'worker', f'delivery.{receipt.state}', job.id, recipient=plan.recipient)


def _apply_publish(db, job):
    """发布组内运行全部成功才一次性切换；任一重算失败则保留旧版。"""
    group = job.publish_group
    if not group:
        # 独立运行：成功即作为自身版本的发布结果。
        job.published = True
        job.published_at = now()
        record(db, 'worker', 'analysis.published', job.id, publish_group=group)
        return
    siblings = list(db.scalars(select(Run).where(Run.publish_group == group)))
    pending = [item for item in siblings if item.status in (Status.QUEUED, Status.RUNNING)]
    if pending:
        record(db, 'worker', 'publish.waiting', job.id, publish_group=group,
               pending=len(pending))
        return
    # 与本次运行同属一个排期版本的，才是这一代要上线的新运行；其余为历史代。
    new_generation = [item for item in siblings
                      if item.activation_version_id == job.activation_version_id]
    failed = [item for item in new_generation if item.status == Status.FAILED]
    if failed:
        record(db, 'worker', 'publish.kept_previous', job.id, publish_group=group,
               failed_runs=[item.id for item in failed])
        return
    switched = rollout_service.switch_published(db, new_generation, group)
    record(db, 'worker', 'publish.switched' if switched else 'publish.kept_previous',
           job.id, publish_group=group, switched=switched)

"""排期版本、更正重算与原子发布切换的集成测试（SQLite 内存库）。"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

# 在导入应用代码前配置环境变量。
_TMP = tempfile.mkdtemp(prefix='rollout-test-')
os.environ.setdefault('DATABASE_URL', 'sqlite://')
os.environ.setdefault('JWT_SECRET', 'test-secret')
os.environ.setdefault('ARTIFACT_DIR', _TMP)
os.environ.setdefault('REDIS_URL', 'redis://localhost:6399/0')

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from src.config import database as db_config  # noqa: E402
from src.config.database import Base  # noqa: E402
from src.constants.enums import Status  # noqa: E402
from src.domain.demo import demo_rows  # noqa: E402
from src.models.jobs import Run  # noqa: E402
from src.models.rollout import ActivationVersion  # noqa: E402
from src.models.source import Dataset  # noqa: E402
from src.services import execution, rollout as rollout_service  # noqa: E402
from src.services.execution import claim, execute  # noqa: E402


def _schedule(day_map):
    return [{'market_id': market, 'activate_date': day} for market, day in day_map.items()]


# 七家市场两批启用，对照市场启用日放到观测期（10-26 ~ 11-15）之后。
V1 = _schedule({
    '南桥': '2026-11-02', '西苑': '2026-11-02', '滨河': '2026-11-09',
    '新街': '2027-01-01', '石桥': '2027-01-01', '北仓': '2027-01-01', '东埠': '2027-01-01',
})
# 更正：滨河由 11-09 提前到 11-02（影响第二批，且第一批曾把滨河当对照）。
V2 = _schedule({
    '南桥': '2026-11-02', '西苑': '2026-11-02', '滨河': '2026-11-02',
    '新街': '2027-01-01', '石桥': '2027-01-01', '北仓': '2027-01-01', '东埠': '2027-01-01',
})
PARAMS = {'window_days': 5, 'pretrend_threshold': 2}


class RolloutPipelineTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            'sqlite://', connect_args={'check_same_thread': False},
            poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(self.engine, expire_on_commit=False)
        # 让应用模块共用同一内存库（模块按名称导入了 Session/engine）。
        db_config.engine = self.engine
        db_config.Session = self.Session
        execution.Session = self.Session

    def _seed_dataset(self):
        with self.Session.begin() as db:
            dataset = Dataset(name='演示批次', filename='demo.csv', checksum='sig-1',
                              rows=demo_rows(), row_count=len(demo_rows()), author='seed')
            db.add(dataset)
        return dataset.id

    def _drain_worker(self):
        while True:
            lease = claim()
            if lease is None:
                return
            execute(lease)

    def test_version_chain_and_active_switch(self):
        dataset_id = self._seed_dataset()
        with self.Session.begin() as db:
            v1, created = rollout_service.create_version(db, dataset_id, '排期v1', V1, 'analyst')
            self.assertTrue(created)
            v2, created = rollout_service.create_version(db, dataset_id, '排期v2', V2, 'analyst', '提前滨河')
            self.assertTrue(created)
            self.assertEqual(v2.version, 2)
            self.assertEqual(v2.supersedes_id, v1.id)
            self.assertFalse(db.get(ActivationVersion, v1.id).is_active)
            self.assertTrue(db.get(ActivationVersion, v2.id).is_active)
            self.assertEqual(rollout_service.affected_markets(v1, v2), ['滨河'])
            # 完全相同的重复提交不产生新版本。
            again, created = rollout_service.create_version(db, dataset_id, '排期v2', V2, 'analyst')
            self.assertFalse(created)
            self.assertEqual(again.id, v2.id)

    def test_run_publishes_then_correction_switches_atomically(self):
        dataset_id = self._seed_dataset()
        with self.Session.begin() as db:
            v1, _ = rollout_service.create_version(db, dataset_id, '排期v1', V1, 'analyst')
            run = Run(name='分批测算', dataset_id=dataset_id, kind='rollout',
                      activation_version_id=v1.id, params=dict(PARAMS),
                      publish_group='report-A', author='analyst')
            db.add(run)
        self._drain_worker()
        with self.Session.begin() as db:
            run = db.scalar(select(Run).where(Run.name == '分批测算'))
            self.assertEqual(run.status, Status.COMPLETE)
            self.assertTrue(run.published)
            self.assertIsNotNone(run.export_file)
            first_id = run.id
            old_batch_dates = [b['activate_date'] for b in run.result['batches']]
            self.assertEqual(len(old_batch_dates), 2)

            v2, _ = rollout_service.create_version(db, dataset_id, '排期v2', V2, 'analyst', '提前滨河')
            impacted = rollout_service.impacted_runs(db, v2)
            self.assertEqual([item.id for item in impacted], [first_id])
            plan = rollout_service.recompute_plan(db, v2)
            self.assertEqual(len(plan), 1)
            self.assertEqual(plan[0]['publish_group'], 'report-A')
            for step in plan:
                db.add(Run(name=step['name'], dataset_id=dataset_id, kind='rollout',
                           activation_version_id=v2.id, params=step['params'],
                           publish_group=step['publish_group'], author='analyst'))

        self._drain_worker()
        with self.Session.begin() as db:
            runs = list(db.scalars(select(Run).order_by(Run.id)))
            old, new = runs[0], runs[1]
            self.assertTrue(new.published)
            self.assertFalse(old.published)  # 一次性切换，旧版下线但记录保留。
            self.assertEqual(new.activation_version_id, old.activation_version_id + 1)
            # 旧版结果仍可查（未被覆盖）。
            self.assertIsNotNone(old.result)
            self.assertEqual([b['activate_date'] for b in old.result['batches']],
                             old_batch_dates)
            # 新版滨河进入第一批。
            first_batch = new.result['batches'][0]
            self.assertIn('滨河', first_batch['treated_markets'])

    def test_failed_recompute_keeps_previous_published(self):
        dataset_id = self._seed_dataset()
        with self.Session.begin() as db:
            v1, _ = rollout_service.create_version(db, dataset_id, '排期v1', V1, 'analyst')
            good = Run(name='正常', dataset_id=dataset_id, kind='rollout',
                       activation_version_id=v1.id, params=dict(PARAMS),
                       publish_group='report-B', author='analyst')
            db.add(good)
        self._drain_worker()
        with self.Session.begin() as db:
            v2, _ = rollout_service.create_version(db, dataset_id, '排期v2', V2, 'analyst')
            plan = rollout_service.recompute_plan(db, v2)
            for step in plan:
                db.add(Run(name=step['name'], dataset_id=dataset_id, kind='rollout',
                           activation_version_id=v2.id,
                           params={'window_days': 99, 'pretrend_threshold': 2},  # 非法参数 → 失败
                           publish_group=step['publish_group'], author='analyst'))
        self._drain_worker()
        with self.Session.begin() as db:
            runs = list(db.scalars(select(Run).order_by(Run.id)))
            old, bad = runs[0], runs[1]
            self.assertEqual(bad.status, Status.FAILED)
            self.assertTrue(old.published)   # 失败保留旧版
            self.assertFalse(bad.published)
            self.assertIsNotNone(old.export_file)

    def test_two_corrections_old_result_never_overwrites_new(self):
        dataset_id = self._seed_dataset()
        with self.Session.begin() as db:
            v1, _ = rollout_service.create_version(db, dataset_id, '排期v1', V1, 'analyst')
            db.add(Run(name='首版', dataset_id=dataset_id, kind='rollout',
                       activation_version_id=v1.id, params=dict(PARAMS),
                       publish_group='report-C', author='analyst'))
        self._drain_worker()
        with self.Session.begin() as db:
            v2, _ = rollout_service.create_version(db, dataset_id, '排期v2', V2, 'analyst')
            v3_map = dict((item['market_id'], item['activate_date']) for item in V2)
            v3_map['北仓'] = '2026-11-09'
            v3, _ = rollout_service.create_version(
                db, dataset_id, '排期v3', _schedule(v3_map), 'analyst', '北仓提前')
            # 两次更正同时提交：重算计划每个发布组只保留一条最新草稿。
            plan = rollout_service.recompute_plan(db, v3)
            self.assertEqual(len(plan), 1)
            for step in plan:
                db.add(Run(name=step['name'], dataset_id=dataset_id, kind='rollout',
                           activation_version_id=v3.id, params=step['params'],
                           publish_group=step['publish_group'], author='analyst'))
        self._drain_worker()
        with self.Session.begin() as db:
            runs = list(db.scalars(select(Run).order_by(Run.id)))
            self.assertEqual(len(runs), 2)  # 首版 + 一次最终重算，没有中间版本回盖。
            self.assertTrue(runs[1].published)
            self.assertFalse(runs[0].published)
            self.assertEqual(runs[1].activation_version_id, runs[0].activation_version_id + 2)


    def test_independent_run_recalc_also_switches(self):
        """独立运行（无发布组）更正重算后，旧版同样被一次性下线。"""
        dataset_id = self._seed_dataset()
        with self.Session.begin() as db:
            v1, _ = rollout_service.create_version(db, dataset_id, '排期v1', V1, 'analyst')
            db.add(Run(name='独立首版', dataset_id=dataset_id, kind='rollout',
                       activation_version_id=v1.id, params=dict(PARAMS),
                       publish_group=None, author='analyst'))
        self._drain_worker()
        with self.Session.begin() as db:
            v2, _ = rollout_service.create_version(db, dataset_id, '排期v2', V2, 'analyst')
            plan = rollout_service.recompute_plan(db, v2)
            self.assertEqual(len(plan), 1)
            group = plan[0]['publish_group']
            for step in plan:
                db.add(Run(name=step['name'], dataset_id=dataset_id, kind='rollout',
                           activation_version_id=v2.id, params=step['params'],
                           publish_group=group, author='analyst'))
                source = db.get(Run, step['source_run_id'])
                source.publish_group = group
        self._drain_worker()
        with self.Session.begin() as db:
            runs = list(db.scalars(select(Run).order_by(Run.id)))
            self.assertFalse(runs[0].published)
            self.assertTrue(runs[1].published)


if __name__ == '__main__':
    unittest.main(verbosity=2)

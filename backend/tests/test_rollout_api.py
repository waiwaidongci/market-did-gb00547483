"""通过 FastAPI TestClient 验证排期版本与重算接口的 HTTP 契约。"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix='rollout-api-')
os.environ.setdefault('DATABASE_URL', 'sqlite://')
os.environ.setdefault('JWT_SECRET', 'test-secret')
os.environ.setdefault('ARTIFACT_DIR', _TMP)
os.environ.setdefault('REDIS_URL', 'redis://localhost:6399/0')
os.environ.setdefault('SEED_DEMO', 'false')

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from src.config import database as db_config  # noqa: E402
from src.config.database import Base  # noqa: E402
from src.domain.demo import demo_rows  # noqa: E402
from src.main import app  # noqa: E402
from src.models.identity import User  # noqa: E402
from src.models.source import Dataset  # noqa: E402
from src.services import execution  # noqa: E402
from src.utils.security import hash_password  # noqa: E402

V1 = [{'market_id': m, 'activate_date': d} for m, d in {
    '南桥': '2026-11-02', '西苑': '2026-11-02', '滨河': '2026-11-09',
    '新街': '2027-01-01', '石桥': '2027-01-01', '北仓': '2027-01-01',
    '东埠': '2027-01-01'}.items()]
V2 = [dict(item, activate_date='2026-11-02') if item['market_id'] == '滨河' else item
      for item in V1]


class RolloutApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine('sqlite://', connect_args={'check_same_thread': False},
                                   poolclass=StaticPool)
        Base.metadata.create_all(cls.engine)
        cls.Session = sessionmaker(cls.engine, expire_on_commit=False)
        db_config.engine = cls.engine
        db_config.Session = cls.Session
        execution.Session = cls.Session
        with cls.Session.begin() as db:
            db.add(User(username='analyst', role='analyst',
                        password_hash=hash_password('analyst-demo-2611')))
            rows = demo_rows()
            db.add(Dataset(name='演示批次', filename='demo.csv', checksum='sig-api',
                           rows=rows, row_count=len(rows), author='seed'))
        cls.client = TestClient(app)
        resp = cls.client.post('/auth/login', json={'username': 'analyst',
                                                    'password': 'analyst-demo-2611'})
        cls.token = resp.json()['token']
        cls.headers = {'Authorization': f'Bearer {cls.token}'}

    def _drain(self):
        while True:
            lease = execution.claim()
            if lease is None:
                return
            execution.execute(lease)

    def test_full_correction_flow_over_http(self):
        # 1. 提交首版排期。
        resp = self.client.post('/activation-versions', headers=self.headers,
                                json={'dataset_id': 1, 'name': '排期v1', 'entries': V1})
        self.assertEqual(resp.status_code, 201, resp.text)
        v1 = resp.json()
        self.assertTrue(v1['is_active'])
        self.assertEqual(v1['version'], 1)

        # 2. 发起分批测算。
        resp = self.client.post('/rollout-runs', headers=self.headers, json={
            'dataset_id': 1, 'name': '分批首版', 'activation_version_id': v1['id'],
            'params': {'window_days': 5, 'pretrend_threshold': 2},
            'publish_group': 'api-group'})
        self.assertEqual(resp.status_code, 202, resp.text)
        self._drain()

        # 3. 页面发布视图只返回已发布运行。
        resp = self.client.get('/published?dataset_id=1', headers=self.headers)
        self.assertEqual(len(resp.json()), 1)
        self.assertTrue(resp.json()[0]['published'])

        # 4. 提交更正版本。
        resp = self.client.post('/activation-versions', headers=self.headers,
                                json={'dataset_id': 1, 'name': '排期v2', 'entries': V2,
                                      'note': '滨河提前'})
        self.assertEqual(resp.status_code, 201, resp.text)
        v2 = resp.json()
        self.assertEqual(v2['affected_markets'], ['滨河'])
        self.assertFalse(self.client.get(
            '/activation-versions?dataset_id=1', headers=self.headers).json()[1]['is_active'])

        # 5. 影响面包含原运行（滨河曾是第一批对照）。
        resp = self.client.get(f"/activation-versions/{v2['id']}/impact", headers=self.headers)
        self.assertEqual(len(resp.json()['impacted_runs']), 1)

        # 6. 触发重算并等待，发布视图一次性切换。
        resp = self.client.post('/recalculations', headers=self.headers,
                                json={'activation_version_id': v2['id']})
        self.assertEqual(resp.status_code, 202, resp.text)
        self._drain()
        published = self.client.get('/published?dataset_id=1', headers=self.headers).json()
        self.assertEqual(len(published), 1)
        self.assertEqual(published[0]['activation_version_id'], v2['id'])

        # 7. 旧运行仍可在完整运行列表中查询（原记录照常可查）。
        all_runs = self.client.get('/runs', headers=self.headers).json()
        self.assertEqual(len(all_runs), 2)
        legacy = next(item for item in all_runs if item['activation_version_id'] == v1['id'])
        self.assertFalse(legacy['published'])

    def test_invalid_schedule_rejected(self):
        bad = [{'market_id': '不存在市场', 'activate_date': '2026-11-02'}]
        resp = self.client.post('/activation-versions', headers=self.headers,
                                json={'dataset_id': 1, 'name': '坏排期', 'entries': bad})
        # 版本可先入库，但发起测算时领域校验会拒绝市场集合不一致。
        self.assertEqual(resp.status_code, 201, resp.text)
        version_id = resp.json()['id']
        resp = self.client.post('/rollout-runs', headers=self.headers, json={
            'dataset_id': 1, 'name': '应失败', 'activation_version_id': version_id,
            'params': {'window_days': 5, 'pretrend_threshold': 2}})
        run_id = resp.json()['id']
        self._drain()
        detail = self.client.get(f'/runs/{run_id}', headers=self.headers).json()
        self.assertEqual(detail['status'], 'failed')
        self.assertIn('不一致', detail['error'])

if __name__ == '__main__':
    unittest.main(verbosity=2)

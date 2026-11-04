"""Run directly with unittest against a disposable maply_test_local_* database."""
import unittest
from unittest.mock import patch
import uuid
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import text

from app import tagging
from app.config import get_settings
from app.db import Base, get_engine, session_scope
from app.local_tagging import ImageRequest, complete, reserve
from app.models import Job, Shot, TagSettings, TagTask


def labels():
    return {'schema_version': 'maply-accessibility/1',
            'footpath': {'value': 'unknown', 'confidence': None,
                         'evidence': 'Test image has no assessable footpath', 'visibility': 'outside_frame'},
            'features': [], 'notes': [], 'review_reasons': []}


class LocalTaggingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        engine = get_engine()
        if not engine.url.database.startswith('maply_test_local_'):
            raise RuntimeError('Tests require a disposable maply_test_local_* database')
        if not get_settings().data_root.is_relative_to(Path('/tmp').resolve()):
            raise RuntimeError('Tests require a throwaway data folder')
        with engine.begin() as conn:
            conn.execute(text('CREATE EXTENSION IF NOT EXISTS postgis'))
        Base.metadata.create_all(engine)

    def setUp(self):
        self.provider = patch('app.tagging.provider_settings', return_value={
            'api_key': 'test-key-never-used', 'openrouter_model': 'test-model'})
        self.provider.start()
        self.addCleanup(self.provider.stop)
        self.jid, self.sid = uuid.uuid4(), uuid.uuid4()
        with session_scope() as s:
            row = tagging.settings_row(s)
            row.parallel_sessions = 20
            job = Job(id=self.jid, name='Test', slug=str(self.jid), streets={}, params={}, batch_size=1)
            s.add(job)
            s.flush()
            s.add(Shot(id=self.sid, job_id=self.jid, code='P1', ordinal=1, batch=1,
                       segment_key='test', street='Test', kind='street', side='left', view_kind='forward',
                       lat=0, lng=0, geom='SRID=4326;POINT(0 0)', heading=0, pitch=0, fov=80,
                       url='https://example.com', filename='missing.png', status='captured',
                       result={'file': 'images/missing.png', 'sha256': 'a' * 64}))

    def request(self, sha='a' * 64):
        return ImageRequest(shot_id=self.sid, image_sha256=sha, worker_id=str(self.sid))

    def claim(self):
        with session_scope() as s:
            return reserve(s, self.request())['task']

    def completion(self, task):
        return tagging.CompleteRequest(worker_id=str(self.sid), lease_token=task['lease_token'],
                                       model=task['model'], labels=labels())

    def test_missing_original_can_be_tagged_and_persisted(self):
        task = self.claim()
        with session_scope() as s:
            complete(s, uuid.UUID(task['id']), self.completion(task), 'a' * 64)
        with session_scope() as s:
            shot = s.get(Shot, self.sid)
            self.assertEqual(shot.labels['provenance']['image_source'], 'checksum_verified_local_backup')
            self.assertEqual(s.get(TagTask, uuid.UUID(task['id'])).status, 'completed')
            label_files = list((get_settings().jobs_dir / str(self.jid) / 'labels').glob('*.json'))
            self.assertEqual(len(label_files), 1)
            self.assertEqual(reserve(s, self.request()), {'reason': 'completed'})

    def test_stale_backup_refused_before_paid_work(self):
        with self.assertRaises(HTTPException), session_scope() as s:
            reserve(s, self.request('b' * 64))

    def test_changed_cloud_photo_refuses_results(self):
        task = self.claim()
        with session_scope() as s:
            s.get(Shot, self.sid).result = {'file': 'images/new.png', 'sha256': 'b' * 64}
        with self.assertRaises(HTTPException), session_scope() as s:
            complete(s, uuid.UUID(task['id']), self.completion(task), 'a' * 64)
        with session_scope() as s:
            self.assertIsNone(s.get(Shot, self.sid).labels)

    def test_expired_lease_and_wrong_local_hash_refuse_results(self):
        task = self.claim()
        with self.assertRaises(HTTPException), session_scope() as s:
            complete(s, uuid.UUID(task['id']), self.completion(task), 'b' * 64)
        with session_scope() as s:
            s.get(TagTask, uuid.UUID(task['id'])).lease_until = tagging.now() - tagging.timedelta(seconds=1)
        with self.assertRaises(HTTPException), session_scope() as s:
            complete(s, uuid.UUID(task['id']), self.completion(task), 'a' * 64)

    def test_global_capacity_prevents_extra_reservations(self):
        self.claim()
        with session_scope() as s:
            active = tagging.active_sessions(s)
            s.get(TagSettings, 1).parallel_sessions = active
        with session_scope() as s:
            self.assertEqual(reserve(s, self.request()), {'reason': 'capacity'})


if __name__ == '__main__':
    unittest.main()

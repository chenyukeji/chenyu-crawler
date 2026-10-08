import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import seller_vat_loop as loop
from crawler.seller_vat import acquire_collection_lock, collection_lock_held, release_collection_lock


class SellerVatSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.status = self.root / 'status.json'

    def test_missing_profile_waits_without_starting_collector(self):
        with patch.object(loop, 'STATUS', self.status), \
             patch.object(loop, 'profile_ready', return_value=False), \
             patch.object(loop.signal, 'signal'), \
             patch.object(loop.subprocess, 'Popen') as popen:
            self.assertEqual(loop.main(['--once']), 0)
        popen.assert_not_called()
        self.assertEqual(json.loads(self.status.read_text())['state'], 'waiting_for_login')
        self.assertFalse((self.root / 'seller_vat.sqlite3').exists())

    def test_hourly_run_uses_headless_chromium_and_records_next_time(self):
        started = datetime.now(loop.TZ)
        fake = Mock()
        fake.wait.return_value = 0
        with patch.object(loop, 'STATUS', self.status), \
             patch.object(loop, 'profile_ready', return_value=True), \
             patch.object(loop, 'profile_stamp', return_value=(('Cookies', 1),)), \
             patch.object(loop, 'collection_lock_held', return_value=False), \
             patch.object(loop, 'run_id_at', return_value='2026-10-08_120000'), \
             patch.object(loop, '_manifest', return_value={'status': 'complete'}), \
             patch.object(loop.signal, 'signal'), \
             patch.object(loop.subprocess, 'Popen', return_value=fake) as popen:
            self.assertEqual(loop.main(['--once']), 0)
        command = popen.call_args.args[0]
        self.assertIn('--headless', command)
        self.assertEqual(command[command.index('--channel') + 1], 'chromium')
        self.assertEqual(command[command.index('--run-id') + 1], '2026-10-08_120000')
        status = json.loads(self.status.read_text())
        self.assertEqual(status['state'], 'scheduled')
        next_run = datetime.fromisoformat(status['next_run_at'])
        self.assertGreaterEqual(next_run, started + timedelta(minutes=59))
        self.assertLess(next_run, started + timedelta(minutes=61))

    def test_login_failure_waits_for_fresh_session(self):
        fake = Mock()
        fake.wait.return_value = 2
        with patch.object(loop, 'STATUS', self.status), \
             patch.object(loop, 'profile_ready', return_value=True), \
             patch.object(loop, 'profile_stamp', return_value=(('Cookies', 1),)), \
             patch.object(loop, 'collection_lock_held', return_value=False), \
             patch.object(loop, 'run_id_at', return_value='2026-10-08_120000'), \
             patch.object(loop, '_manifest', return_value={'status': 'blocked', 'error': '卖家精灵尚未登录'}), \
             patch.object(loop.signal, 'signal'), \
             patch.object(loop.subprocess, 'Popen', return_value=fake):
            self.assertEqual(loop.main(['--once']), 2)
        self.assertEqual(json.loads(self.status.read_text())['state'], 'waiting_for_login')

    def test_busy_browser_retries_soon_after_interactive_login(self):
        fake = Mock()
        fake.wait.return_value = 2
        with patch.object(loop, 'STATUS', self.status), \
             patch.object(loop, 'profile_ready', return_value=True), \
             patch.object(loop, 'profile_stamp', return_value=(('Cookies', 1),)), \
             patch.object(loop, 'collection_lock_held', return_value=False), \
             patch.object(loop, 'run_id_at', return_value='2026-10-08_120000'), \
             patch.object(loop, '_manifest', return_value={}), \
             patch.object(loop.signal, 'signal'), \
             patch.object(loop.subprocess, 'Popen', return_value=fake):
            self.assertEqual(loop.main(['--once']), 2)
        status = json.loads(self.status.read_text())
        self.assertEqual(status['state'], 'retry_wait')
        self.assertIn('浏览器', status['message'])

    def test_run_ids_do_not_overwrite_existing_batches(self):
        started = datetime(2026, 10, 8, 12, 0, tzinfo=loop.TZ)
        (self.root / '2026-10-08_120000').mkdir()
        self.assertEqual(loop.run_id_at(started, self.root), '2026-10-08_120000_2')

    @unittest.skipUnless(os.name == 'posix', 'flock is used on Linux')
    def test_process_lock_blocks_overlap_and_releases_without_deleting_file(self):
        path = self.root / 'auto-collection.lock'
        first = acquire_collection_lock(path)
        self.assertIsNotNone(first)
        try:
            self.assertTrue(collection_lock_held(path))
            self.assertIsNone(acquire_collection_lock(path))
        finally:
            release_collection_lock(path, first)
        self.assertFalse(collection_lock_held(path))
        second = acquire_collection_lock(path)
        self.assertIsNotNone(second)
        release_collection_lock(path, second)

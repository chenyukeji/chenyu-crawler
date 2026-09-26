import io
import json
import signal
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

from crawler.amazon import ParseError, _extract_card, deduplicate_items
from crawler.lifecycle import (CollectorLock, CollectorBusy, SHANGHAI, due_sources,
                               next_retry_time, recover_interrupted, latest_runs)
from database.repository import SnapshotStore
from database.schema import COLLECTION_RUNS_TABLE_SQL, initialize_schema
import run_daily
import scheduler_loop


class ReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = SnapshotStore(self.root / 'collector.db')
        self.now = datetime(2026, 9, 24, 8, 0, tzinfo=SHANGHAI)
        self.source = dict(marketplace='DE', category='beauty', url='https://www.amazon.de/gp/new-releases/beauty', enabled=True)

    def start(self, status='PARTIAL', retry_round=0, now=None, source=None):
        now = now or self.now
        source = source or self.source
        rid = self.store.start_run(source_url=source['url'], marketplace=source['marketplace'], category=source['category'],
                                   snapshot_date=now.date().isoformat(), started_at=now.isoformat(), retry_round=retry_round)
        self.store.finish_run(rid, status=status, item_count=99 if status=='PARTIAL' else 0, now=now)
        return rid

    def due(self, now, sources=None):
        with closing(self.store.connect()) as c:
            return due_sources(c, sources or [self.source], now)

    def test_retry_rounds_persist_and_stop_after_two(self):
        self.start()
        self.store = SnapshotStore(self.store.path)  # simulate reopening after restart
        self.assertEqual(self.due(self.now + timedelta(minutes=14)), [])
        self.assertEqual(len(self.due(self.now + timedelta(minutes=15))), 1)
        retry_one = self.now + timedelta(minutes=20)
        self.start(retry_round=1, now=retry_one)
        self.assertEqual(self.due(retry_one + timedelta(minutes=59)), [])
        self.assertEqual(len(self.due(retry_one + timedelta(minutes=60))), 1)
        self.start(retry_round=2, now=retry_one + timedelta(minutes=65))
        self.assertEqual(self.due(self.now + timedelta(hours=5)), [])

    def test_success_disabled_and_blocked_are_not_retried(self):
        self.start()
        self.assertEqual(self.due(self.now + timedelta(hours=1), [dict(self.source, enabled=False)]), [])
        self.start(status='COMPLETE', now=self.now + timedelta(minutes=1))
        self.assertEqual(self.due(self.now + timedelta(hours=1)), [])
        for status in ['BLOCKED', 'SKIPPED']:
            self.assertIsNone(next_retry_time(status, '2026-09-24', 0, self.now))

    def test_block_in_same_market_does_not_suppress_other_sources(self):
        self.start()
        other = dict(self.source,category='kitchen',url='https://www.amazon.de/gp/new-releases/kitchen')
        self.start(status='BLOCKED',source=other)
        self.assertEqual(self.due(self.now+timedelta(hours=1)), [self.source])

    def test_retries_never_cross_snapshot_date(self):
        now = self.now.replace(hour=23, minute=50)
        self.assertIsNone(next_retry_time('PARTIAL','2026-09-24',0,now))
        self.start()
        self.assertEqual(self.due(self.now+timedelta(days=1)), [])

    def test_unknown_rank_is_not_invented_or_written(self):
        rows=[dict(asin=f'B{i:09d}',title='Product',rank=r,image_url='image') for i,r in enumerate([None,0,-1,1.5,True])]
        self.assertEqual(deduplicate_items(rows,100), [])
        with self.assertRaises(ValueError):
            self.store.ingest(rows,source_url=self.source['url'],marketplace='DE',category='beauty',snapshot_date='2026-09-24')
        with patch('crawler.amazon._read_text',side_effect=['/dp/B000000001','Product','','','','','']):
            with self.assertRaisesRegex(ParseError,'MISSING_RANK'):
                _extract_card(MagicMock(),self.source['url'])

    def test_outer_real_rank_is_preserved(self):
        with patch('crawler.amazon._read_text',side_effect=['/dp/B000000001','Product','','','','','#51','','','','image']):
            self.assertEqual(_extract_card(MagicMock(),self.source['url'])['rank'],51)

    def test_real_process_kill_releases_lock_and_recovers_running_rows(self):
        script = '''
import sys,time
from pathlib import Path
from crawler.lifecycle import CollectorLock
from database.repository import SnapshotStore
p=Path(sys.argv[1])
with CollectorLock(p/'collector.lock'):
 s=SnapshotStore(p/'collector.db')
 s.start_run(source_url='https://www.amazon.de/gp/new-releases/beauty',marketplace='DE',category='beauty',snapshot_date='2026-09-24',started_at='2026-09-24T08:00:00+08:00')
 print('ready',flush=True)
 time.sleep(60)
'''
        p=subprocess.Popen([sys.executable,'-u','-c',script,str(self.root)],stdout=subprocess.PIPE,text=True)
        try:
            self.assertEqual(p.stdout.readline().strip(),'ready')
            with self.assertRaises(CollectorBusy):
                with CollectorLock(self.root/'collector.lock'):
                    pass
            p.kill();p.wait(timeout=5)
            with CollectorLock(self.root/'collector.lock'):
                self.assertEqual(recover_interrupted(self.store,self.now),1)
                self.assertEqual(recover_interrupted(self.store,self.now),0)
            with closing(self.store.connect()) as c:
                row=c.execute('SELECT status,next_retry_at FROM collection_runs').fetchone()
            self.assertEqual(tuple(row),('INTERRUPTED','2026-09-24T08:15:00+08:00'))
        finally:
            if p.poll() is None:p.kill();p.wait()
            p.stdout.close()

    def test_legacy_lock_file_does_not_block_new_collector(self):
        path=self.root/'collector.lock';path.write_text('2026-09-24T06:00:00+08:00')
        with CollectorLock(path):
            self.assertIn('pid',json.loads(path.read_text()))
        with CollectorLock(path):
            pass

    def test_old_schema_migration_is_idempotent_and_retains_evidence(self):
        path=self.root/'legacy.db'
        old=COLLECTION_RUNS_TABLE_SQL.replace("'QUEUED', ", "").replace("'RUNNING', 'COMPLETE', 'PARTIAL', 'BLOCKED', 'SKIPPED', 'PARSE_ERROR', 'FAILED', 'INTERRUPTED'", "'RUNNING', 'COMPLETE', 'FAILED'").replace(',\n    retry_round INTEGER NOT NULL DEFAULT 0,\n    next_retry_at TEXT','')
        with closing(sqlite3.connect(path)) as c:
            c.execute(old)
            errors=[('partial',99,'只采集到 99/100 条'),('blocked',0,'ACCESS_BLOCKED: unauthorized ai agent'),('skipped',0,'未请求：同站点已明确访问受限'),('failed',0,'timeout')]
            for rid,count,error in errors:
                c.execute("INSERT INTO collection_runs VALUES (?,?, 'DE',?,'2026-09-24','2026-09-24T06:00:00+08:00',NULL,'FAILED',?,?)",(rid,rid,rid,count,error))
            c.commit();initialize_schema(c);initialize_schema(c)
            result=c.execute('SELECT run_id,status,item_count,error_message FROM collection_runs ORDER BY run_id').fetchall()
            self.assertEqual([r[1] for r in result],['BLOCKED','FAILED','PARTIAL','SKIPPED'])
            self.assertEqual(result[2][2:],(99,'只采集到 99/100 条'))
            self.assertEqual(c.execute('PRAGMA integrity_check').fetchone()[0],'ok')

    def test_scheduler_selects_due_retry_and_releases_thread_lock(self):
        self.start()
        with patch('scheduler_loop.configured_now',return_value=(self.now+timedelta(minutes=15),'06:00')), \
             patch('scheduler_loop.recover_abandoned_runs',return_value=0), \
             patch('scheduler_loop.claim_daily_run',return_value=False), \
             patch('scheduler_loop.load_config',return_value={'sources':[self.source]}), \
             patch('scheduler_loop.connect_database',side_effect=self.store.connect), \
             patch('scheduler_loop.run_daily',return_value=2) as run:
            self.assertTrue(scheduler_loop.check_once())
            run.assert_called_once_with(retry_due=True,scheduled=False)
        self.assertFalse(scheduler_loop.RUN_LOCK.locked())

    def test_timeout_terminates_process_group_and_recovers(self):
        process=MagicMock(pid=12345,returncode=-9)
        process.communicate.side_effect=[subprocess.TimeoutExpired('collector',1),subprocess.TimeoutExpired('collector',10),('','')]
        with patch('scheduler_loop.subprocess.Popen',return_value=process), patch('scheduler_loop.os.killpg') as kill, patch('scheduler_loop.recover_abandoned_runs') as recover:
            self.assertEqual(scheduler_loop.run_daily(timeout=1),124)
            self.assertEqual(kill.call_args_list[0].args,(12345,signal.SIGTERM))
            self.assertEqual(kill.call_args_list[1].args,(12345,signal.SIGKILL))
            recover.assert_called_once()

    def test_runner_stores_checkpoint_but_keeps_blocked_status(self):
        from crawler.amazon import AccessControlBlocked
        config = self.root / 'sources.json'
        config.write_text(json.dumps({'sources': [self.source], 'daily_schedule': '06:00'}))
        items = [dict(asin='B000000001', title='Product', image_url='image', rank=51)]
        blocked = AccessControlBlocked('AGENT_RESTRICTED: page 2', partial_snapshot={'items': items})
        args = ['--config', str(config), '--db', str(self.store.path), '--date', '2026-09-24']
        with patch('run_daily.ROOT', self.root), patch('playwright.sync_api.sync_playwright'), \
             patch('run_daily.collect_with_retry', side_effect=blocked), redirect_stdout(io.StringIO()):
            self.assertEqual(run_daily.main(args), 2)
        with closing(self.store.connect()) as c:
            row = c.execute('SELECT status,item_count,next_retry_at FROM collection_runs').fetchone()
            self.assertEqual(tuple(row), ('BLOCKED', 1, None))
            self.assertEqual(c.execute('SELECT rank FROM observations').fetchone()[0], 51)

    def test_runner_persists_completed_site_short_reason_without_retry(self):
        source = dict(self.source, marketplace='US', category='lawn-garden',
                      url='https://www.amazon.com/gp/new-releases/lawn-garden')
        config = self.root / 'sources.json'
        config.write_text(json.dumps({'sources': [source], 'daily_schedule': '06:00'}))
        reason = '网站未展示第50名；其余99个真实排名已采集至末页'
        items = [dict(asin=f'B{rank:09d}', rank=rank, title='Product',
                      image_url='https://example.test/image.png')
                 for rank in range(1, 101) if rank != 50]
        snapshot = dict(status='ok', items=items, error_message=reason,
                        top_list_complete=True, pages_visited=2,
                        attempts=[], page_diagnostics=[], target_items=99)
        args = ['--config', str(config), '--db', str(self.store.path),
                '--date', '2026-09-24']
        with patch('run_daily.ROOT', self.root),              patch('playwright.sync_api.sync_playwright'),              patch('run_daily.collect_with_retry', return_value=snapshot),              redirect_stdout(io.StringIO()):
            self.assertEqual(run_daily.main(args), 0)
        with closing(self.store.connect()) as connection:
            row = connection.execute(
                'SELECT status,item_count,error_message,next_retry_at '
                'FROM collection_runs WHERE category=?', ('lawn-garden',),
            ).fetchone()
        self.assertEqual(tuple(row), ('COMPLETE', 99, reason, None))

    def test_runner_attempts_each_category_after_another_is_restricted(self):
        from crawler.amazon import AccessControlBlocked
        first_us = dict(self.source, marketplace='US', category='baby-products', url='https://www.amazon.com/gp/new-releases/baby-products')
        second_us = dict(self.source, marketplace='US', category='fashion', url='https://www.amazon.com/gp/new-releases/fashion')
        de = dict(self.source, marketplace='DE', category='baby', url='https://www.amazon.de/gp/new-releases/baby')
        config = self.root / 'sources.json'
        config.write_text(json.dumps({'sources': [first_us, second_us, de], 'daily_schedule': '06:00'}))
        blocked = AccessControlBlocked(
            'ACCESS_BLOCKED: AGENT_RESTRICTED',
            diagnostics={'kind': 'AGENT_RESTRICTED'},
        )
        snap = {
            'status': 'ok', 'items': [], 'error_message': '', 'top_list_complete': True,
            'pages_visited': 1, 'attempts': [], 'page_diagnostics': [],
        }
        args = ['--config', str(config), '--db', str(self.store.path), '--date', '2026-09-24']
        with patch('run_daily.ROOT', self.root), patch('playwright.sync_api.sync_playwright'), \
             patch('run_daily.collect_with_retry', side_effect=[blocked, snap, snap]) as collect, \
             redirect_stdout(io.StringIO()):
            self.assertEqual(run_daily.main(args), 2)
        self.assertEqual(collect.call_count, 3)
        with closing(self.store.connect()) as c:
            rows = c.execute(
                'SELECT marketplace,category,status,error_message FROM collection_runs ORDER BY rowid'
            ).fetchall()
        self.assertEqual([tuple(row[:3]) for row in rows], [
            ('US', 'baby-products', 'BLOCKED'),
            ('US', 'fashion', 'COMPLETE'),
            ('DE', 'baby', 'COMPLETE'),
        ])
        self.assertIsNone(rows[1]['error_message'])

    def test_runner_delayed_retry_filters_sources_and_persists_next_round(self):
        self.start()
        config=self.root/'sources.json';config.write_text(json.dumps({'sources':[self.source],'daily_schedule':'06:00'}))
        snap={'status':'partial','items':[dict(asin='B000000001',title='Product',image_url='image',rank=1)],'error_message':'missing 2-100','top_list_complete':False,'pages_visited':1,'attempts':[],'page_diagnostics':[]}
        args=['--config',str(config),'--db',str(self.store.path),'--date','2026-09-24','--retry-due']
        with patch('run_daily.ROOT',self.root),patch('run_daily.now_shanghai',return_value=self.now+timedelta(minutes=15)),patch('database.repository.now_shanghai',return_value=self.now+timedelta(minutes=16)),patch('playwright.sync_api.sync_playwright'),patch('run_daily.collect_with_retry',return_value=snap) as collect,redirect_stdout(io.StringIO()):
            self.assertEqual(run_daily.main(args),2)
            self.assertEqual(collect.call_count,1)
            self.assertEqual(run_daily.main(args),0) # not due a second time
            self.assertEqual(collect.call_count,1)
        with closing(self.store.connect()) as c:
            latest=latest_runs(c,'2026-09-24')[self.source['url']]
        self.assertEqual((latest['status'],latest['retry_round'],latest['next_retry_at']),('PARTIAL',1,'2026-09-24T09:16:00+08:00'))

    def test_interrupted_runner_records_terminal_status(self):
        config=self.root/'sources.json';config.write_text(json.dumps({'sources':[self.source]}))
        with patch('run_daily.ROOT',self.root),patch('playwright.sync_api.sync_playwright'),patch('run_daily.collect_with_retry',side_effect=KeyboardInterrupt('stop')),redirect_stdout(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                run_daily.main(['--config',str(config),'--db',str(self.store.path),'--date','2026-09-24'])
        with closing(self.store.connect()) as c:
            self.assertEqual(c.execute('SELECT status FROM collection_runs').fetchone()[0],'INTERRUPTED')
        with CollectorLock(self.root/'collector.lock'):
            pass

if __name__=='__main__':unittest.main()

import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from crawler.amazon import AccessControlBlocked
from crawler.lifecycle import SHANGHAI, latest_runs, recover_interrupted, CollectorLock, cancel_unavailable_queue
from database.repository import SnapshotStore
from database.schema import COLLECTION_RUNS_TABLE_SQL, initialize_schema
from api.main import today_source_cards
import run_daily
import scheduler_loop


class CardQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.store=SnapshotStore(self.root/'data.db')
        self.now=datetime(2026,9,24,14,0,tzinfo=SHANGHAI)
        self.sources=[dict(marketplace='US',category=name,url='https://www.amazon.com/gp/new-releases/'+name,enabled=True) for name in ['fashion','beauty']]
        self.config={'sources':self.sources,'daily_schedule':'06:00','collection_policy':{'max_items_per_source':100,'retention_days':7}}
        self.config_path=self.root/'sources.json';self.config_path.write_text(json.dumps(self.config))

    def enqueue(self,source=None):
        s=source or self.sources[0]
        return self.store.enqueue_run(source_url=s['url'],marketplace=s['marketplace'],category=s['category'],snapshot_date=self.now.date().isoformat(),started_at=self.now.isoformat())

    def cards(self,now=None):
        with patch('api.main.connect_database',side_effect=self.store.connect):
            return today_source_cards(self.config,now or self.now)

    def test_waiting_running_complete_states_are_distinct(self):
        self.assertEqual(self.cards()[0]['today_status'],'WAITING')
        rid=self.enqueue();self.assertEqual(self.cards()[0]['today_status'],'QUEUED')
        self.assertFalse(self.cards()[0]['can_run'])
        self.assertTrue(self.store.mark_running(rid));self.assertFalse(self.store.mark_running(rid))
        self.assertEqual(self.cards()[0]['today_status'],'RUNNING')
        self.store.finish_run(rid,status='COMPLETE',item_count=100,now=self.now)
        self.assertEqual(self.cards()[0]['today_status'],'COMPLETE')
        self.assertTrue(self.cards()[0]['can_run'])
        self.assertEqual(self.cards(self.now+timedelta(days=1))[0]['today_status'],'WAITING')

    def test_duplicate_requests_are_idempotent_and_survive_reopen(self):
        first=self.enqueue();self.assertEqual(first,self.enqueue())
        self.store=SnapshotStore(self.store.path)
        self.assertEqual(first,self.enqueue())
        with CollectorLock(self.root/'collector.lock'):
            self.assertEqual(recover_interrupted(self.store,self.now),0)
        self.store.mark_running(first);self.assertEqual(first,self.enqueue())
        with closing(self.store.connect()) as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM collection_runs').fetchone()[0],1)

    def test_manual_request_during_another_job_queues_only_target(self):
        self.assertTrue(scheduler_loop.RUN_LOCK.acquire(blocking=False))
        try:
            with patch('scheduler_loop.CONFIG_PATH',self.config_path),patch('scheduler_loop.DEFAULT_DB_PATH',self.store.path),patch('scheduler_loop.now_shanghai',return_value=self.now):
                self.assertTrue(scheduler_loop.start_manual_run('US_beauty'))
                self.assertTrue(scheduler_loop.start_manual_run('US_beauty'))
                with self.assertRaises(ValueError):scheduler_loop.start_manual_run('unknown')
            with closing(self.store.connect()) as c:
                self.assertEqual([tuple(r) for r in c.execute('SELECT category,status FROM collection_runs')],[('beauty','QUEUED')])
        finally:scheduler_loop.RUN_LOCK.release()

    def test_blocked_category_does_not_skip_next_category(self):
        seen=[]
        def collect(page,source,*args,**kwargs):
            with closing(self.store.connect()) as c:
                seen.append([tuple(r) for r in c.execute('SELECT category,status FROM collection_runs ORDER BY category')])
            if source['category']=='fashion':raise AccessControlBlocked('ACCESS_BLOCKED: test')
            return dict(status='ok',items=[dict(rank=i,asin=f'B{i:09d}',title='Product',image_url='image') for i in range(1,101)],error_message='',top_list_complete=True,pages_visited=2,attempts=[],page_diagnostics=[])
        with patch('run_daily.ROOT',self.root),patch('run_daily.now_shanghai',return_value=self.now),patch('playwright.sync_api.sync_playwright'),patch('run_daily.collect_with_retry',side_effect=collect) as crawl,patch('run_daily.time.sleep'),redirect_stdout(io.StringIO()):
            self.assertEqual(run_daily.main(['--config',str(self.config_path),'--db',str(self.store.path)]),2)
        self.assertEqual(crawl.call_count,2)
        self.assertEqual(seen[0],[('beauty','QUEUED'),('fashion','RUNNING')])
        self.assertEqual(seen[1],[('beauty','RUNNING'),('fashion','BLOCKED')])
        with closing(self.store.connect()) as c:
            rows=latest_runs(c,'2026-09-24')
        self.assertEqual(rows[self.sources[1]['url']]['status'],'COMPLETE')

    def test_removed_or_previous_day_queue_is_cancelled(self):
        rid=self.enqueue();cancel_unavailable_queue(self.store,[],self.now)
        with closing(self.store.connect()) as c:self.assertEqual(c.execute('SELECT status FROM collection_runs WHERE run_id=?',(rid,)).fetchone()[0],'SKIPPED')
        self.enqueue(self.sources[1]);cancel_unavailable_queue(self.store,self.sources,self.now+timedelta(days=1))
        with closing(self.store.connect()) as c:self.assertEqual(c.execute("SELECT COUNT(*) FROM collection_runs WHERE status='QUEUED'").fetchone()[0],0)

    def test_queued_worker_only_runs_requested_source(self):
        self.enqueue(self.sources[1])
        with patch('run_daily.ROOT',self.root),patch('run_daily.now_shanghai',return_value=self.now),patch('playwright.sync_api.sync_playwright'),patch('run_daily.collect_with_retry',side_effect=AccessControlBlocked('test')) as crawl,redirect_stdout(io.StringIO()):
            run_daily.main(['--config',str(self.config_path),'--db',str(self.store.path),'--queued'])
        self.assertEqual(crawl.call_count,1)
        self.assertEqual(crawl.call_args.args[1]['category'],'beauty')

    def test_queue_migration_preserves_retry_schedule(self):
        with closing(sqlite3.connect(self.root/'v2.db')) as c:
            c.execute(COLLECTION_RUNS_TABLE_SQL.replace("'QUEUED', ",''))
            c.execute("INSERT INTO collection_runs VALUES ('one','url','DE','beauty','2026-09-24','2026-09-24T12:00:00+08:00','2026-09-24T12:01:00+08:00','PARTIAL',99,'missing rank',1,'2026-09-24T13:01:00+08:00')")
            c.commit();before=c.execute('SELECT * FROM collection_runs').fetchall()
            initialize_schema(c);initialize_schema(c)
            self.assertEqual(before,c.execute('SELECT * FROM collection_runs').fetchall())

    def test_one_manual_category_does_not_cancel_rest_of_daily_schedule(self):
        self.enqueue()
        with patch('scheduler_loop.CONFIG_PATH',self.config_path),patch('scheduler_loop.connect_database',side_effect=self.store.connect):
            self.assertTrue(scheduler_loop.claim_daily_run(self.now,'06:00'))
            self.enqueue(self.sources[1])
            self.assertFalse(scheduler_loop.claim_daily_run(self.now,'06:00'))

if __name__=='__main__':unittest.main()

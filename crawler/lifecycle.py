"""Persistent retry policy and process-safe collector ownership (Linux host)."""
from __future__ import annotations

import fcntl
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

SHANGHAI = timezone(timedelta(hours=8), name='Asia/Shanghai')
RETRY_DELAYS = (15, 60)
RETRYABLE_STATUSES = {'PARTIAL', 'PARSE_ERROR', 'FAILED', 'INTERRUPTED'}
TERMINAL_STATUSES = RETRYABLE_STATUSES | {'COMPLETE', 'BLOCKED', 'SKIPPED'}


def now_shanghai():
    return datetime.now(SHANGHAI)


def next_retry_time(status, snapshot_date, retry_round, now):
    if status not in RETRYABLE_STATUSES or retry_round >= len(RETRY_DELAYS):
        return None
    target = now.astimezone(SHANGHAI) + timedelta(minutes=RETRY_DELAYS[retry_round])
    if target.date().isoformat() != snapshot_date:
        return None
    return target.isoformat(timespec='seconds')


class CollectorBusy(RuntimeError):
    pass


class CollectorLock:
    """The kernel releases flock even after SIGKILL. Never unlink its inode."""
    def __init__(self, path: Path):
        self.path = Path(path)
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open('a+', encoding='utf-8')
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.handle.close()
            self.handle = None
            raise CollectorBusy('Another collector owns the database lock') from exc
        self.handle.seek(0)
        self.handle.truncate()
        self.handle.write(json.dumps({'pid': os.getpid(), 'started_at': now_shanghai().isoformat()}))
        self.handle.flush()
        return self

    def __exit__(self, *exc):
        if self.handle:
            self.handle.seek(0)
            self.handle.truncate()
            self.handle.flush()
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None


def recover_interrupted(store, now=None):
    """Caller MUST hold CollectorLock; no live compliant collector can exist."""
    now = now or now_shanghai()
    from contextlib import closing
    with closing(store.connect()) as connection:
        rows = connection.execute("SELECT run_id,item_count FROM collection_runs WHERE status='RUNNING'").fetchall()
    for row in rows:
        store.finish_run(row[0], status='INTERRUPTED', item_count=row[1],
                         error_message='INTERRUPTED: 采集进程已退出，恢复遗留任务；已保存快照不变', now=now)
    return len(rows)


def latest_runs(connection, snapshot_date):
    rows = connection.execute('''
        SELECT * FROM collection_runs r WHERE snapshot_date=? AND r.rowid=(
            SELECT n.rowid FROM collection_runs n
            WHERE n.snapshot_date=r.snapshot_date AND n.source_url=r.source_url
            ORDER BY n.started_at DESC,n.rowid DESC LIMIT 1)
    ''', (snapshot_date,)).fetchall()
    return {r['source_url']: dict(r) for r in rows}


def due_sources(connection, sources, now):
    rows = latest_runs(connection, now.astimezone(SHANGHAI).date().isoformat())
    due = []
    for source in sources:
        row = rows.get(source['url'])
        if not source.get('enabled', True) or not row:
            continue
        if row['status'] not in RETRYABLE_STATUSES or row['retry_round'] >= len(RETRY_DELAYS):
            continue
        if row['next_retry_at'] and datetime.fromisoformat(row['next_retry_at']) <= now:
            due.append(source)
    return due


def queued_sources(connection, sources, now):
    rows = latest_runs(connection, now.astimezone(SHANGHAI).date().isoformat())
    return sorted([s for s in sources if s.get('enabled',True) and rows.get(s['url'],{}).get('status')=='QUEUED'],
                  key=lambda s: rows[s['url']]['started_at'])


def cancel_unavailable_queue(store, sources, now):
    from contextlib import closing
    allowed = {s['url'] for s in sources if s.get('enabled',True)}
    with closing(store.connect()) as c:
        rows = c.execute("SELECT run_id,source_url,snapshot_date FROM collection_runs WHERE status='QUEUED'").fetchall()
    for row in rows:
        if row['source_url'] not in allowed or row['snapshot_date'] != now.astimezone(SHANGHAI).date().isoformat():
            store.finish_run(row['run_id'],status='SKIPPED',item_count=0,error_message='排队请求已取消：类目停用、删除或已跨日',now=now)

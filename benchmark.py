"""Benchmark the local automation engine, not LLM extraction quality or latency."""
import argparse
import json
from pathlib import Path
import platform
import sqlite3
import statistics
import tempfile
import time

from actionweaver import Ledger, parse_json


def run(count=500):
    source = Path('example-notes.txt').read_text()
    plan = parse_json(Path('example-plan.json').read_text())
    runs = []
    for _ in range(3):
        with tempfile.TemporaryDirectory() as directory, Ledger(Path(directory)/'benchmark.sqlite') as ledger:
            ids = [ledger.stage(source+f'\nFixture {i}', plan, 'synthetic-fixture') for i in range(count)]
            for digest in ids:
                ledger.decide(digest, 'SIMULATED-BENCHMARK-REVIEWER')
            start = time.perf_counter()
            created = sum(ledger.apply(digest) for digest in ids)
            elapsed = time.perf_counter()-start
            start = time.perf_counter()
            retried = sum(ledger.apply(digest) for digest in ids)
            retry_seconds = time.perf_counter()-start
            actual = ledger.db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]
            if created != count*3 or retried != 0 or actual != count*3:
                raise AssertionError('idempotency or creation count invariant failed')
            runs.append({'apply_seconds': elapsed, 'retry_seconds': retry_seconds, 'created_tasks': actual, 'duplicate_tasks': retried})
    return {'mode': 'synthetic local ledger benchmark; no LLM inference; approvals simulated',
            'python': platform.python_version(), 'sqlite': sqlite3.sqlite_version,
            'platform': platform.platform(), 'proposals_per_run': count, 'tasks_per_run': count*3,
            'runs': runs, 'median_apply_seconds': statistics.median(r['apply_seconds'] for r in runs),
            'median_retry_seconds': statistics.median(r['retry_seconds'] for r in runs),
            'scope': 'file-backed temporary SQLite WAL, default synchronous mode; excludes inference, stage, approval and export'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = json.dumps(run(), indent=2)+'\n'
    if args.output:
        args.output.write_text(result)
    print(result, end='')

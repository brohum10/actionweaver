"""Offline fixture demonstration. No LLM call; approval is simulated, not a human review."""
import json
from pathlib import Path
import tempfile

from actionweaver import Ledger, parse_json


def run():
    source = Path('example-notes.txt').read_text()
    plan = parse_json(Path('example-plan.json').read_text())
    with tempfile.TemporaryDirectory() as directory:
        with Ledger(Path(directory)/'demo.sqlite') as ledger:
            digest = ledger.stage(source, plan, 'hand-authored-fixture')
            ledger.decide(digest, 'SIMULATED-DEMO-REVIEWER')
            first = ledger.apply(digest)
            replay = ledger.apply(digest)
            ledger.complete(digest, 't1', 'SIMULATED-DEMO-REVIEWER')
            return {'mode': 'offline fixture; no LLM used; approval simulated',
                    'first_apply_created': first, 'retry_created': replay, 'board': ledger.board(digest)}


if __name__ == '__main__':
    print(json.dumps(run(), indent=2))

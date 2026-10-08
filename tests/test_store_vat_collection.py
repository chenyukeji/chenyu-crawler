import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import store_vat_collection as collection


class StoreVatCollectionTests(unittest.TestCase):
    def test_worker_scans_all_pages_then_checks_only_new_stores(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = 'eu-store-20261008-101010-000001'
            folder = root / run_id
            folder.mkdir()
            status_file = root / 'status.json'
            status_file.write_text(json.dumps({'run_id': run_id, 'state': 'queued'}))
            commands = []

            def execute(command, output):
                commands.append(command)
                if 'discover_recent_buybox_stores.py' in command[1]:
                    (output / '店铺候选.json').write_text(json.dumps({'stores': [
                        {'seller_id': 'A1111111111'}, {'seller_id': 'A2222222222'}]}))

            with patch.object(collection, 'REPORTS', root), patch.object(collection, 'STATUS', status_file), \
                 patch.object(collection, '_known_store_ids', return_value={'A1111111111'}), \
                 patch.object(collection, 'acquire_collection_lock', return_value=42), \
                 patch.object(collection, 'release_collection_lock'), \
                 patch.object(collection, '_execute', side_effect=execute):
                collection.worker(run_id)
                result = collection.status()
            self.assertEqual(result['state'], 'complete')
            self.assertEqual(result['new_stores'], 1)
            self.assertEqual(json.loads((folder / '新店铺ID.json').read_text()), ['A2222222222'])
            self.assertEqual(len(commands), 2)
            self.assertNotIn('--max-pages', commands[0])
            self.assertIn('--seller-ids-file', commands[1])


if __name__ == '__main__':
    unittest.main()

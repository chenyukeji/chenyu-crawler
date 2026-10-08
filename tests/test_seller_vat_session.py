import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from crawler.seller_vat import restore_sellersprite_session, save_sellersprite_session


class SellerVatSessionTests(unittest.TestCase):
    def test_private_session_is_scoped_to_account_and_sellersprite(self):
        with tempfile.TemporaryDirectory() as folder:
            profile = Path(folder)
            context = Mock()
            context.cookies.return_value = [
                {'name': 'session', 'value': 'private-cookie', 'domain': '.sellersprite.com', 'expires': -1},
                {'name': 'other', 'value': 'unrelated', 'domain': '.example.com', 'expires': -1},
            ]
            save_sellersprite_session(context, profile, 'first-account')
            path = profile / 'sellersprite-session.json'
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            state = json.loads(path.read_text())
            self.assertEqual([item['name'] for item in state['cookies']], ['session'])
            self.assertNotIn('password', state)
            restored = Mock()
            restore_sellersprite_session(restored, profile, 'second-account')
            restored.add_cookies.assert_not_called()
            restore_sellersprite_session(restored, profile, 'first-account')
            restored.add_cookies.assert_called_once_with(state['cookies'])

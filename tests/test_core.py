"""Pure-logic regression checks; intentionally independent from Telegram network/aiogram."""
import ast
import asyncio
import json
import logging
import os
from pathlib import Path
import re
import tempfile
import types
import unicodedata
import unittest

ROOT = Path(__file__).resolve().parents[1]
TREE = ast.parse((ROOT / 'main.py').read_text(encoding='utf-8'))


def isolated_functions(names, **extra):
    nodes = [n for n in TREE.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
    ns = {'Path': Path, 'os': os, 'json': json, 'logging': logging,
          're': re, 'unicodedata': unicodedata, 'CallbackQuery': object, **extra}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(ROOT / 'main.py'), 'exec'), ns)
    return ns


class LogicTests(unittest.TestCase):
    def test_answer_normalization(self):
        ns = isolated_functions(['normalize_answer', 'normalized_aliases'])
        self.assertEqual(ns['normalize_answer']('  ВОНЁН  '), 'вонен')
        self.assertEqual(ns['normalized_aliases'](['Ынчэ', 'ынчэ', 'Eunchae']), ['ынчэ', 'eunchae'])

    def test_guess_supports_russian_from_intuition(self):
        ns = isolated_functions(['normalize_answer', 'normalized_aliases', 'build_guess_aliases'])
        lines = (ROOT / 'groups/intuitioninfo.txt').read_text(encoding='utf-8').splitlines()
        descriptions = []
        for line in lines:
            match = re.search(r'\{(.*?)\}', line)
            if match:
                descriptions.append({'aliases': ns['normalized_aliases'](match.group(1).split('/'))})
        ns['INTUITION_LINES'] = descriptions
        ns['ALL_GUESS'] = [{'folder': folder.name} for folder in (ROOT / 'photos').iterdir() if folder.is_dir()]
        ns['ALIASES'] = {'eunchae': ['eunchae', 'ынче', 'ынчэ'], 'winter': ['winter']}
        ns['read_json'] = lambda *args: {}
        ns['ALIASES_FILE'] = 'aliases.json'
        aliases = ns['build_guess_aliases']()
        self.assertGreaterEqual(len(aliases), 13)
        for folder, russian_name in [('eunchae', 'ынче'), ('chaewon', 'чэвон'),
                                    ('kazuha', 'кадзуха'), ('iroha', 'ироха'),
                                    ('wonyoung', 'вонен'), ('minju', 'минджу')]:
            with self.subTest(folder=folder):
                self.assertIn(russian_name, aliases[folder])

    def test_callback_payload_rejected_when_invalid(self):
        ns = isolated_functions(['decode_moderation_callback'])
        decode = ns['decode_moderation_callback']
        self.assertEqual(decode('approve_join:-10001234:123', 'approve_join'), (-10001234, 123))
        self.assertIsNone(decode('approve_join:-1:123:extra', 'approve_join'))
        self.assertIsNone(decode('approve_join:abc:123', 'approve_join'))
        self.assertIsNone(decode('unmute:-1:123', 'approve_join'))

    def test_state_writes_atomically(self):
        ns = isolated_functions(['state_file', 'read_json', 'write_json'])
        with tempfile.TemporaryDirectory() as directory:
            ns['DATA_DIR'] = Path(directory)
            ns['write_json']('test.json', {'имя': '<admin>', 'score': 5})
            self.assertEqual(ns['read_json']('test.json', {}), {'имя': '<admin>', 'score': 5})
            self.assertFalse((Path(directory) / 'test.json.tmp').exists())
            self.assertEqual(ns['read_json']('absent.json', []), [])

    def test_admin_callback_must_be_private_and_authorized(self):
        ns = isolated_functions(['require_admin_callback'], is_admin=lambda uid: uid == 42)
        async def simulate(user_id, chat_id, chat_type):
            calls = []
            async def answer(*args, **kwargs):
                calls.append((args, kwargs))
            callback = types.SimpleNamespace(
                from_user=types.SimpleNamespace(id=user_id),
                message=types.SimpleNamespace(chat=types.SimpleNamespace(id=chat_id, type=chat_type)),
                answer=answer,
            )
            permitted = await ns['require_admin_callback'](callback)
            return permitted, calls
        permitted, _ = asyncio.run(simulate(42, 42, 'private'))
        self.assertTrue(permitted)
        for args in [(5, 5, 'private'), (42, 100, 'private'), (42, -100, 'supergroup')]:
            permitted, calls = asyncio.run(simulate(*args))
            self.assertFalse(permitted)
            self.assertEqual(len(calls), 1)


if __name__ == '__main__':
    unittest.main()

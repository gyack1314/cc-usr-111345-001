"""资料读取、身份与边界检查。"""
import copy
import unittest
from pathlib import Path
from subquery_data.io import load, decode_rows, DataError
from subquery_data.domain import inspect_tables

class MaterialTests(unittest.TestCase):
    def test_registered_tables(self):
        tables = load('data/normal.json')
        self.assertTrue(inspect_tables(tables)['identity_checked'])
        self.assertEqual(set(tables), set(['requests', 'settlements', 'query_cases']))

    def test_boundary_material(self):
        tables = load('data/boundary.json')
        self.assertTrue(inspect_tables(tables)['identity_checked'])

    def test_content_identity_rejected(self):
        with self.assertRaisesRegex(DataError, '摘要'):
            load('data/broken-digest.json')

    def test_duplicate_identity_rejected(self):
        with self.assertRaisesRegex(DataError, '重复'):
            decode_rows(b'{"id":"a"}\n{"id":"a"}\n', ['id'])

    def test_missing_fields_rejected(self):
        with self.assertRaisesRegex(DataError, '字段'):
            decode_rows(b'{"id":"a"}\n', ['id','value'])

    def test_missing_table_rejected(self):
        tables = load('data/normal.json')
        del tables[next(iter(tables))]
        with self.assertRaisesRegex(DataError, '集合'):
            inspect_tables(tables)

if __name__ == '__main__':
    unittest.main()

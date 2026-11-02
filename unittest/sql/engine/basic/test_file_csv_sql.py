"""Read-only CSV SQL integration checks against a dedicated seekdb instance.

Set FILE_SQL_TEST_PORT and FILE_SQL_TEST_CSV to the instance port and the
absolute server-side CSV path. The file must be within secure_file_priv.
"""
import datetime
import os
from pathlib import Path
import tempfile
import unittest

import pymysql


class FileCsvSqlTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.path = os.environ['FILE_SQL_TEST_CSV']
        cls.parquet_path = os.environ.get('FILE_SQL_TEST_PARQUET')
        cls.db = pymysql.connect(host='127.0.0.1', port=int(os.environ['FILE_SQL_TEST_PORT']),
                                 user='root', charset='utf8mb4', autocommit=True,
                                 connect_timeout=10, read_timeout=30, write_timeout=10)
        cls.cursor = cls.db.cursor()

    @classmethod
    def tearDownClass(cls):
        cls.db.close()

    def setUp(self):
        self.cursor.execute('set session enable_file_sql = off')

    def test_default_disabled(self):
        connection = pymysql.connect(host='127.0.0.1', port=int(os.environ['FILE_SQL_TEST_PORT']),
                                     user='root', charset='utf8mb4', autocommit=True,
                                     connect_timeout=10, read_timeout=30, write_timeout=10)
        try:
            cursor = connection.cursor()
            cursor.execute('select @@session.enable_file_sql')
            self.assertEqual((0,), cursor.fetchone())
            with self.assertRaises(pymysql.Error):
                cursor.execute('select count(*) from read_csv(%s)', (self.path,))
            with self.assertRaises(pymysql.Error):
                cursor.execute('select count(*) from read_json(%s)', (str(Path(self.path).with_name('basic.jsonl')),))
            if self.parquet_path:
                with self.assertRaises(pymysql.Error):
                    cursor.execute('select count(*) from read_parquet(%s)', (self.parquet_path,))
            with self.assertRaises(pymysql.Error):
                cursor.execute('select count(*) from glob(%s)', (str(Path(self.path).parent / '*.csv'),))
        finally:
            connection.close()

    def test_ordinary_sql_still_works(self):
        self.cursor.execute('select 1 + 2, upper(\'seekdb\')')
        self.assertEqual((3, 'SEEKDB'), self.cursor.fetchone())

    def test_projection_filter_and_order(self):
        self.cursor.execute('set session enable_file_sql = on')
        self.cursor.execute('select id,name from read_csv(%s) f where id >= 2 order by id', (self.path,))
        self.assertEqual(((2, 'Bob'), (3, 'Chen')), self.cursor.fetchall())

    def test_aggregate_and_options(self):
        self.cursor.execute('set session enable_file_sql = on')
        self.cursor.execute('select count(*),sum(id) from read_csv(%s,header=true,sample_size=-1)', (self.path,))
        self.assertEqual((3, 6), self.cursor.fetchone())

    def test_boolean_date_and_datetime(self):
        self.cursor.execute('set session enable_file_sql = on')
        self.cursor.execute('select active,day,created from read_csv(%s) order by id', (self.path,))
        self.assertEqual(((1, datetime.date(2026, 8, 1), datetime.datetime(2026, 8, 1, 10, 11, 12)),
                          (0, None, None),
                          (1, datetime.date(2026, 8, 3), datetime.datetime(2026, 8, 3, 12, 13, 14))),
                         self.cursor.fetchall())

    def test_quoted_multiline_and_duplicate_header(self):
        self.cursor.execute('set session enable_file_sql = on')
        self.cursor.execute('select note,id__2 from read_csv(%s) order by id', (self.path,))
        self.assertEqual((('hello,\nworld', 101), ('plain', 102), ('say "hi"', 103)), self.cursor.fetchall())

    def test_two_file_join_and_aliases(self):
        self.cursor.execute('set session enable_file_sql = on')
        self.cursor.execute('select l.id,r.name from read_csv(%s) l '
                            'join read_csv(%s) r on l.id=r.id where l.id=2',
                            (self.path, self.path))
        self.assertEqual(((2, 'Bob'),), self.cursor.fetchall())

    def test_jsonl_union_schema_and_join(self):
        path = str(Path(self.path).with_name('basic.jsonl'))
        self.cursor.execute('set session enable_file_sql = on')
        self.cursor.execute("select id,name,score,active,day,note from read_json(%s,format='newline_delimited') order by id", (path,))
        self.assertEqual(((1, 'Alice', 98.5, 1, None, None),
                          (2, 'Bob', None, 0, datetime.date(2026, 8, 2), None),
                          (3, 'Chen', 90.0, 1, None, 'ok')), self.cursor.fetchall())
        self.cursor.execute('select c.id,j.name from read_csv(%s) c join read_json(%s) j on c.id=j.id where c.id=2',
                            (self.path, path))
        self.assertEqual(((2, 'Bob'),), self.cursor.fetchall())

    def test_parquet_v2_snappy_aggregate_and_option_rejection(self):
        if not self.parquet_path:
            self.skipTest('FILE_SQL_TEST_PARQUET is not set')
        self.cursor.execute('set session enable_file_sql = on')
        self.cursor.execute('select count(*), min(c1), max(c1), sum(c1) from read_parquet(%s)',
                            (self.parquet_path,))
        self.assertEqual((7, 0, 6, 21), self.cursor.fetchone())
        with self.assertRaises(pymysql.Error):
            self.cursor.execute('select count(*) from read_parquet(%s, header = true)', (self.parquet_path,))

    def test_jsonl_strings_null_and_invalid_input(self):
        self.cursor.execute('set session enable_file_sql = on')
        with tempfile.TemporaryDirectory(prefix='seekdb-jsonl-regression-') as directory:
            path = Path(directory) / 'values.jsonl'
            path.write_text('{"id":1,"v":""}\n{"id":2,"v":null}\n{"id":3,"v":"x\\ny"}\n', encoding='utf-8')
            self.cursor.execute('select id,v from read_json(%s) order by id', (str(path),))
            self.assertEqual(((1, ''), (2, None), (3, 'x\ny')), self.cursor.fetchall())
            path.write_text('{"id":1}\nnot-json\n', encoding='utf-8')
            with self.assertRaises(pymysql.Error):
                self.cursor.execute('select count(*) from read_json(%s)', (str(path),))

    def test_glob_finds_files_and_handles_empty_matches(self):
        self.cursor.execute('set session enable_file_sql = on')
        with tempfile.TemporaryDirectory(prefix='seekdb-glob-regression-') as directory:
            root = Path(directory)
            (root / 'one.csv').write_text('id\n1\n', encoding='utf-8')
            (root / 'two.csv').write_text('id\n2\n', encoding='utf-8')
            (root / 'ignored.txt').write_text('x', encoding='utf-8')
            (root / 'child').mkdir()
            (root / 'child' / 'nested.csv').write_text('id\n3\n', encoding='utf-8')
            (root / 'escape.csv').symlink_to('/etc/passwd')
            (root / 'escape-dir').symlink_to('/etc', target_is_directory=True)
            self.cursor.execute('select file from glob(%s) order by file', (str(root / '*.csv'),))
            self.assertEqual(((str(root / 'one.csv'),), (str(root / 'two.csv'),)), self.cursor.fetchall())
            self.cursor.execute('select file from glob(%s)', (str(root / '*' / '*.csv'),))
            self.assertEqual(((str(root / 'child' / 'nested.csv'),),), self.cursor.fetchall())
            self.cursor.execute('select count(*) from glob(%s)', (str(root / '*.missing'),))
            self.assertEqual((0,), self.cursor.fetchone())
            self.cursor.execute('select count(*) from glob(%s)', (str(root / '*' / 'passwd'),))
            self.assertEqual((0,), self.cursor.fetchone())
            self.cursor.execute('select count(*) from glob(%s) a join glob(%s) b on a.file=b.file',
                                (str(root / '*.csv'), str(root / '*.csv')))
            self.assertEqual((2,), self.cursor.fetchone())

    def test_copy_csv_export_and_reject_existing(self):
        self.cursor.execute('set session enable_file_sql = on')
        with tempfile.TemporaryDirectory(prefix='seekdb-copy-regression-') as directory:
            path = Path(directory) / 'export.csv'
            self.cursor.execute("copy (select id,name,score from read_csv(%s) order by id) "
                                "to %s (format csv, header, nullstr 'NULL')", (self.path, str(path)))
            self.cursor.execute('select id,name,score from read_csv(%s) order by id', (str(path),))
            self.assertEqual(((1, 'Alice', 98.5), (2, 'Bob', 87.0), (3, 'Chen', 90.25)),
                             self.cursor.fetchall())
            original = path.read_bytes()
            with self.assertRaises(pymysql.Error):
                self.cursor.execute("copy (select 1) to %s (format csv)", (str(path),))
            self.assertEqual(original, path.read_bytes())
            second = Path(directory) / 'second.csv'
            self.cursor.execute("copy (select id,name,score from read_csv(%s) order by id) "
                                "to %s (format csv, header, nullstr 'NULL')", (self.path, str(second)))
            self.assertEqual(original, second.read_bytes())
            escaped = Path(directory) / 'escaped.csv'
            self.cursor.execute("copy (select NULL as a, '' as b, 'NULL' as c) "
                                "to %s (format csv, header, nullstr 'NULL')", (str(escaped),))
            self.assertEqual(b'a,b,c\nNULL,"","NULL"\n', escaped.read_bytes())

    def test_copy_default_disabled(self):
        with tempfile.TemporaryDirectory(prefix='seekdb-copy-disabled-') as directory:
            path = Path(directory) / 'must-not-exist.csv'
            with self.assertRaises(pymysql.Error):
                self.cursor.execute('copy (select 1) to %s (format csv)', (str(path),))
            self.assertFalse(path.exists())

    def test_left_join_preserves_unmatched_rows(self):
        self.cursor.execute('set session enable_file_sql = on')
        self.cursor.execute('select l.id,r.name from read_csv(%s) l '
                            'left join read_csv(%s) r on l.id=r.id and r.id=2 order by l.id',
                            (self.path, self.path))
        self.assertEqual(((1, None), (2, 'Bob'), (3, None)), self.cursor.fetchall())

    def test_missing_file_and_unsupported_sample(self):
        self.cursor.execute('set session enable_file_sql = on')
        with self.assertRaises(pymysql.Error):
            self.cursor.execute('select count(*) from read_csv(%s)', (self.path + '.missing',))
        with self.assertRaises(pymysql.Error):
            self.cursor.execute('select count(*) from read_csv(%s,sample_size=1)', (self.path,))

    def test_changed_schema_never_returns_old_types(self):
        self.cursor.execute('set session enable_file_sql = on')
        with tempfile.TemporaryDirectory(prefix='seekdb-file-sql-regression-') as directory:
            path = Path(directory) / 'changing.csv'
            path.write_text('id\n12\n', encoding='utf-8')
            sql = 'select id from read_csv(%s)'
            self.cursor.execute(sql, (str(path),))
            self.assertEqual(((12,),), self.cursor.fetchall())
            path.write_text('id\nchanged-to-text\n', encoding='utf-8')
            try:
                self.cursor.execute(sql, (str(path),))
                self.assertEqual((('changed-to-text',),), self.cursor.fetchall())
            except pymysql.Error:
                # Rejecting a stale cached plan is acceptable; wrong data is not.
                self.cursor.execute('select 1')
                self.assertEqual((1,), self.cursor.fetchone())

    def test_invalid_csv_is_rejected(self):
        self.cursor.execute('set session enable_file_sql = on')
        with tempfile.TemporaryDirectory(prefix='seekdb-file-sql-regression-') as directory:
            path = Path(directory) / 'invalid.csv'
            path.write_text('id,name\n1,alice,extra\n', encoding='utf-8')
            with self.assertRaises(pymysql.Error):
                self.cursor.execute('select count(*) from read_csv(%s)', (str(path),))

    def test_more_than_inline_schema_capacity(self):
        self.cursor.execute('set session enable_file_sql = on')
        with tempfile.TemporaryDirectory(prefix='seekdb-file-sql-regression-') as directory:
            path = Path(directory) / 'wide.csv'
            path.write_text(','.join('c%d' % i for i in range(16)) + '\n' +
                            ','.join(str(i) for i in range(16)) + '\n', encoding='utf-8')
            self.cursor.execute('select c0,c15 from read_csv(%s)', (str(path),))
            self.assertEqual(((0, 15),), self.cursor.fetchall())

    @unittest.skipUnless(os.getenv('FILE_SQL_TEST_ALLOW_PRIV_CHANGE') == '1',
                         'requires a dedicated instance; temporarily changes secure_file_priv')
    def test_secure_file_priv_and_symlink_escape(self):
        socket = os.getenv('FILE_SQL_TEST_SOCKET')
        if not socket:
            self.skipTest('FILE_SQL_TEST_SOCKET is required to change secure_file_priv')
        admin = pymysql.connect(unix_socket=socket, user='root', charset='utf8mb4', autocommit=True)
        self.addCleanup(admin.close)
        admin_cursor = admin.cursor()
        admin_cursor.execute('select @@global.secure_file_priv')
        original = admin_cursor.fetchone()[0]
        with tempfile.TemporaryDirectory(prefix='seekdb-file-sql-priv-') as directory:
            root = Path(directory)
            (root / 'allowed.csv').write_text('id\n1\n', encoding='utf-8')
            (root / 'allowed.jsonl').write_text('{"id":1}\n', encoding='utf-8')
            (root / 'escape.csv').symlink_to('/etc/passwd')
            (root / 'escape-dir').symlink_to('/etc', target_is_directory=True)
            changed = False
            try:
                admin_cursor.execute('set global secure_file_priv = %s', (str(root),))
                changed = True
                probe = pymysql.connect(host='127.0.0.1', port=int(os.environ['FILE_SQL_TEST_PORT']),
                                        user='root', charset='utf8mb4', autocommit=True,
                                        connect_timeout=10, read_timeout=30, write_timeout=10)
                try:
                    cursor = probe.cursor()
                    cursor.execute('set session enable_file_sql = on')
                    cursor.execute('select count(*) from read_csv(%s)', (str(root / 'allowed.csv'),))
                    self.assertEqual((1,), cursor.fetchone())
                    cursor.execute('select count(*) from read_json(%s)', (str(root / 'allowed.jsonl'),))
                    self.assertEqual((1,), cursor.fetchone())
                    cursor.execute('select count(*) from glob(%s)', (str(root / '*.csv'),))
                    self.assertEqual((1,), cursor.fetchone())
                    with self.assertRaises(pymysql.Error):
                        cursor.execute('select count(*) from glob(%s)', ('/etc/*',))
                    for target in ('/etc/seekdb-copy-denied.csv', str(root / 'escape-dir' / 'seekdb-copy-denied.csv')):
                        with self.subTest(copy_target=target), self.assertRaises(pymysql.Error):
                            cursor.execute('copy (select 1) to %s (format csv)', (target,))
                    for path in (str(root / 'escape.csv'), '/etc/passwd', str(root / '../outside.csv')):
                        with self.subTest(path=path), self.assertRaises(pymysql.Error):
                            cursor.execute('select count(*) from read_csv(%s)', (path,))
                        with self.subTest(json_path=path), self.assertRaises(pymysql.Error):
                            cursor.execute('select count(*) from read_json(%s)', (path,))
                finally:
                    probe.close()
            finally:
                if changed:
                    admin_cursor.execute('set global secure_file_priv = %s', (original,))


if __name__ == '__main__':
    unittest.main(verbosity=2)

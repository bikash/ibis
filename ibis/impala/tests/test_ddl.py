# Copyright 2014 Cloudera Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from copy import copy
import gc

import ibis


from posixpath import join as pjoin
import pytest

from ibis.expr.tests.mocks import MockConnection
from ibis.compat import unittest
from ibis.impala import ddl
from ibis.impala.compat import HS2Error, ImpylaError
from ibis.impala.client import build_ast
from ibis.impala.tests.common import IbisTestEnv, ImpalaE2E, connect_test
from ibis.tests.util import assert_equal
import ibis.common as com
import ibis.util as util


ENV = IbisTestEnv()


class TestDropTable(unittest.TestCase):

    def test_must_exist(self):
        statement = ddl.DropTable('foo', database='bar', must_exist=True)
        query = statement.compile()
        expected = "DROP TABLE bar.`foo`"
        assert query == expected

        statement = ddl.DropTable('foo', database='bar', must_exist=False)
        query = statement.compile()
        expected = "DROP TABLE IF EXISTS bar.`foo`"
        assert query == expected


class TestInsert(unittest.TestCase):

    def setUp(self):
        self.con = MockConnection()
        self.t = self.con.table('functional_alltypes')

    def test_select_basics(self):
        name = 'testing123456'

        expr = self.t.limit(10)
        select, _ = _get_select(expr)

        stmt = ddl.InsertSelect(name, select, database='foo')
        result = stmt.compile()

        expected = """\
INSERT INTO foo.`testing123456`
SELECT *
FROM functional_alltypes
LIMIT 10"""
        assert result == expected

        stmt = ddl.InsertSelect(name, select, database='foo', overwrite=True)
        result = stmt.compile()

        expected = """\
INSERT OVERWRITE foo.`testing123456`
SELECT *
FROM functional_alltypes
LIMIT 10"""
        assert result == expected

    def test_select_overwrite(self):
        pass


class TestCacheTable(unittest.TestCase):

    def test_pool_name(self):
        statement = ddl.CacheTable('foo', database='bar')
        query = statement.compile()
        expected = "ALTER TABLE bar.`foo` SET CACHED IN 'default'"
        assert query == expected

        statement = ddl.CacheTable('foo', database='bar', pool='my_pool')
        query = statement.compile()
        expected = "ALTER TABLE bar.`foo` SET CACHED IN 'my_pool'"
        assert query == expected


class TestCreateTable(unittest.TestCase):

    def setUp(self):
        self.con = MockConnection()

        self.t = t = self.con.table('functional_alltypes')
        self.expr = t[t.bigint_col > 0]

    def test_create_external_table_as(self):
        path = '/path/to/table'
        select = build_ast(self.con.table('test1')).queries[0]
        statement = ddl.CTAS('another_table',
                             select,
                             external=True,
                             can_exist=False,
                             path=path,
                             database='foo')
        result = statement.compile()

        expected = """\
CREATE EXTERNAL TABLE foo.`another_table`
STORED AS PARQUET
LOCATION '{0}'
AS
SELECT *
FROM test1""".format(path)
        assert result == expected

    def test_create_table_with_location(self):
        path = '/path/to/table'
        schema = ibis.schema([('foo', 'string'),
                              ('bar', 'int8'),
                              ('baz', 'int16')])
        statement = ddl.CreateTableWithSchema('another_table', schema,
                                              ddl.NoFormat(),
                                              can_exist=False,
                                              path=path, database='foo')
        result = statement.compile()

        expected = """\
CREATE TABLE foo.`another_table`
(`foo` string,
 `bar` tinyint,
 `baz` smallint)
LOCATION '{0}'""".format(path)
        assert result == expected

    def test_create_table_like_parquet(self):
        directory = '/path/to/'
        path = '/path/to/parquetfile'
        statement = ddl.CreateTableParquet('new_table',
                                           directory,
                                           example_file=path,
                                           can_exist=True,
                                           database='foo')

        result = statement.compile()
        expected = """\
CREATE EXTERNAL TABLE IF NOT EXISTS foo.`new_table`
LIKE PARQUET '{0}'
STORED AS PARQUET
LOCATION '{1}'""".format(path, directory)

        assert result == expected

    def test_create_table_parquet_like_other(self):
        # alternative to "LIKE PARQUET"
        directory = '/path/to/'
        example_table = 'db.other'

        statement = ddl.CreateTableParquet('new_table',
                                           directory,
                                           example_table=example_table,
                                           can_exist=True,
                                           database='foo')

        result = statement.compile()
        expected = """\
CREATE EXTERNAL TABLE IF NOT EXISTS foo.`new_table`
LIKE {0}
STORED AS PARQUET
LOCATION '{1}'""".format(example_table, directory)

        assert result == expected

    def test_create_table_parquet_with_schema(self):
        directory = '/path/to/'

        schema = ibis.schema([('foo', 'string'),
                              ('bar', 'int8'),
                              ('baz', 'int16')])

        statement = ddl.CreateTableParquet('new_table',
                                           directory,
                                           schema=schema,
                                           external=True,
                                           can_exist=True,
                                           database='foo')

        result = statement.compile()
        expected = """\
CREATE EXTERNAL TABLE IF NOT EXISTS foo.`new_table`
(`foo` string,
 `bar` tinyint,
 `baz` smallint)
STORED AS PARQUET
LOCATION '{0}'""".format(directory)

        assert result == expected

    def test_create_table_delimited(self):
        path = '/path/to/files/'
        schema = ibis.schema([('a', 'string'),
                              ('b', 'int32'),
                              ('c', 'double'),
                              ('d', 'decimal(12,2)')])

        stmt = ddl.CreateTableDelimited('new_table', path, schema,
                                        delimiter='|',
                                        escapechar='\\',
                                        lineterminator='\0',
                                        database='foo',
                                        can_exist=True)

        result = stmt.compile()
        expected = """\
CREATE EXTERNAL TABLE IF NOT EXISTS foo.`new_table`
(`a` string,
 `b` int,
 `c` double,
 `d` decimal(12,2))
ROW FORMAT DELIMITED
FIELDS TERMINATED BY '|'
ESCAPED BY '\\'
LINES TERMINATED BY '\0'
LOCATION '{0}'""".format(path)
        assert result == expected

    def test_create_external_table_avro(self):
        path = '/path/to/files/'

        avro_schema = {
            'fields': [
                {'name': 'a', 'type': 'string'},
                {'name': 'b', 'type': 'int'},
                {'name': 'c', 'type': 'double'},
                {"type": "bytes",
                 "logicalType": "decimal",
                 "precision": 4,
                 "scale": 2,
                 'name': 'd'}
            ],
            'name': 'my_record',
            'type': 'record'
        }

        stmt = ddl.CreateTableAvro('new_table', path, avro_schema,
                                   database='foo', can_exist=True)

        result = stmt.compile()
        expected = """\
CREATE EXTERNAL TABLE IF NOT EXISTS foo.`new_table`
STORED AS AVRO
LOCATION '%s'
TBLPROPERTIES ('avro.schema.literal'='{
  "fields": [
    {
      "name": "a",
      "type": "string"
    },
    {
      "name": "b",
      "type": "int"
    },
    {
      "name": "c",
      "type": "double"
    },
    {
      "logicalType": "decimal",
      "name": "d",
      "precision": 4,
      "scale": 2,
      "type": "bytes"
    }
  ],
  "name": "my_record",
  "type": "record"
}')""" % path
        assert result == expected

    def test_create_table_parquet(self):
        statement = _create_table('some_table', self.expr,
                                  database='bar',
                                  can_exist=False)
        result = statement.compile()

        expected = """\
CREATE TABLE bar.`some_table`
STORED AS PARQUET
AS
SELECT *
FROM functional_alltypes
WHERE `bigint_col` > 0"""
        assert result == expected

    def test_no_overwrite(self):
        statement = _create_table('tname', self.expr, can_exist=True)
        result = statement.compile()

        expected = """\
CREATE TABLE IF NOT EXISTS `tname`
STORED AS PARQUET
AS
SELECT *
FROM functional_alltypes
WHERE `bigint_col` > 0"""
        assert result == expected

    def test_avro_other_formats(self):
        statement = _create_table('tname', self.t, format='avro',
                                  can_exist=True)
        result = statement.compile()
        expected = """\
CREATE TABLE IF NOT EXISTS `tname`
STORED AS AVRO
AS
SELECT *
FROM functional_alltypes"""
        assert result == expected

        self.assertRaises(ValueError, _create_table, 'tname', self.t,
                          format='foo')

    def test_partition_by(self):
        pass


class TestDDLOperations(ImpalaE2E, unittest.TestCase):

    def test_list_databases(self):
        assert len(self.con.list_databases()) > 0

    def test_list_tables(self):
        assert len(self.con.list_tables(database=self.test_data_db)) > 0
        assert len(self.con.list_tables(like='*nat*',
                                        database=self.test_data_db)) > 0

    def test_set_database(self):
        # create new connection with no default db set
        env = copy(ENV)
        env.test_data_db = None
        con = connect_test(env)
        self.assertRaises(Exception, con.table, 'functional_alltypes')
        con.set_database(self.test_data_db)
        con.table('functional_alltypes')

    def test_tables_robust_to_set_database(self):
        db_name = '__ibis_test_{0}'.format(util.guid())

        self.con.create_database(db_name)
        self.temp_databases.append(db_name)

        table = self.con.table('functional_alltypes')

        self.con.set_database(db_name)

        # it still works!
        table.limit(10).execute()

    def test_create_exists_drop_database(self):
        tmp_name = '__ibis_test_{0}'.format(util.guid())

        assert not self.con.exists_database(tmp_name)

        self.con.create_database(tmp_name)
        assert self.con.exists_database(tmp_name)

        self.con.drop_database(tmp_name)
        assert not self.con.exists_database(tmp_name)

    def test_exists_table(self):
        assert self.con.exists_table('functional_alltypes')
        assert not self.con.exists_table(util.guid())

    def text_exists_table_with_database(self):
        table_name = _random_table_name()
        tmp_db = self.test_data_db
        self.con.create_table(table_name, self.alltypes, database=tmp_db)

        assert self.con.exists_table(table_name, database=tmp_db)

        tmp_name = '__ibis_test_{0}'.format(util.guid())
        self.con.create_database(tmp_name)
        self.temp_databases.append(tmp_name)
        assert not self.con.exists_table(table_name, database=tmp_name)

    def test_create_exists_drop_view(self):
        tmp_name = util.guid()

        assert not self.con.exists_table(tmp_name)

        expr = (self.con.table('functional_alltypes')
                .group_by('string_col')
                .size())

        self.con.create_view(tmp_name, expr)
        self.temp_views.append(tmp_name)
        assert self.con.exists_table(tmp_name)

        # just check it works for now
        expr2 = self.con.table(tmp_name)
        expr2.execute()

        self.con.drop_view(tmp_name)
        assert not self.con.exists_table(tmp_name)

    def test_drop_non_empty_database(self):
        tmp_db = '__ibis_test_{0}'.format(util.guid())

        self.con.create_database(tmp_db)

        self.con.create_table(util.guid(), self.alltypes, database=tmp_db)

        # Has a view, too
        self.con.create_view(util.guid(), self.alltypes,
                             database=tmp_db)

        self.assertRaises(com.IntegrityError, self.con.drop_database, tmp_db)

        self.con.drop_database(tmp_db, force=True)
        assert not self.con.exists_database(tmp_db)

    def test_create_database_with_location(self):
        base = pjoin(self.tmp_dir, util.guid())
        name = '__ibis_test_{0}'.format(util.guid())
        tmp_path = pjoin(base, name)

        self.con.create_database(name, path=tmp_path)
        assert self.hdfs.exists(base)
        self.con.drop_database(name)
        self.hdfs.rmdir(base)

    @pytest.mark.superuser
    def test_create_table_with_location(self):
        base = pjoin(self.tmp_dir, util.guid())
        name = 'test_{0}'.format(util.guid())
        tmp_path = pjoin(base, name)

        # impala user has trouble writing to jenkins-owned dir so here we give
        # the tmp dir 777
        superuser_hdfs = ibis.hdfs_connect(host=ENV.nn_host,
                                           port=ENV.webhdfs_port,
                                           auth_mechanism=ENV.auth_mechanism,
                                           verify=(ENV.auth_mechanism
                                                   not in ['GSSAPI', 'LDAP']),
                                           user=ENV.hdfs_superuser)
        superuser_hdfs.mkdir(base)
        superuser_hdfs.chmod(base, '777')

        expr = self.alltypes
        table_name = _random_table_name()

        self.con.create_table(table_name, expr=expr, path=tmp_path,
                              database=self.test_data_db)
        self.temp_tables.append('.'.join([self.test_data_db, table_name]))
        assert self.hdfs.exists(tmp_path)

    def test_drop_table_not_exist(self):
        random_name = util.guid()
        self.assertRaises(Exception, self.con.drop_table, random_name)

        self.con.drop_table(random_name, force=True)

    def test_truncate_table(self):
        expr = self.alltypes.limit(50)

        table_name = util.guid()
        self.con.create_table(table_name, expr=expr)
        self.temp_tables.append(table_name)

        try:
            self.con.truncate_table(table_name)
        except HS2Error as e:
            if 'AnalysisException' in e.args[0]:
                pytest.skip('TRUNCATE not available in this '
                            'version of Impala')

        result = self.con.table(table_name).execute()
        assert len(result) == 0

    def test_ctas_from_table_expr(self):
        expr = self.alltypes
        table_name = _random_table_name()
        db = self.test_data_db

        self.con.create_table(table_name, expr, database=db)
        self.temp_tables.append('.'.join((db, table_name)))

    def test_create_empty_table(self):
        schema = ibis.schema([('a', 'string'),
                              ('b', 'timestamp'),
                              ('c', 'decimal(12,8)'),
                              ('d', 'double')])

        table_name = util.guid()
        self.con.create_table(table_name, schema=schema)
        self.temp_tables.append(table_name)

        result_schema = self.con.get_schema(table_name)
        assert_equal(result_schema, schema)

        assert len(self.con.table(table_name).execute()) == 0

    def test_insert_table(self):
        expr = self.alltypes
        table_name = _random_table_name()
        db = self.test_data_db

        self.con.create_table(table_name, expr.limit(0), database=db)
        self.temp_tables.append('.'.join((db, table_name)))

        self.con.insert(table_name, expr.limit(10), database=db)

        # check using ImpalaTable.insert
        t = self.con.table(table_name, database=db)
        t.insert(expr.limit(10))

        sz = t.count()
        assert sz.execute() == 20

        # Overwrite and verify only 10 rows now
        t.insert(expr.limit(10), overwrite=True)
        assert sz.execute() == 10

    def test_insert_validate_types(self):
        # GH #235
        table_name = _random_table_name()
        db = self.test_data_db

        expr = self.alltypes
        self.con.create_table(table_name,
                              schema=expr['tinyint_col', 'int_col',
                                          'string_col'].schema(),
                              database=db)
        self.temp_tables.append('.'.join((db, table_name)))

        t = self.con.table(table_name, database=db)

        to_insert = expr[expr.tinyint_col, expr.smallint_col.name('int_col'),
                         expr.string_col]
        t.insert(to_insert.limit(10))

        to_insert = expr[expr.tinyint_col,
                         expr.smallint_col.cast('int32').name('int_col'),
                         expr.string_col]
        t.insert(to_insert.limit(10))

        to_insert = expr[expr.tinyint_col,
                         expr.bigint_col.name('int_col'),
                         expr.string_col]
        with self.assertRaises(com.IbisError):
            t.insert(to_insert.limit(10))

    def test_compute_stats(self):
        self.con.table('functional_alltypes').compute_stats()

    def test_drop_table_or_view(self):
        t = self.db.functional_alltypes

        tname = util.guid()
        self.con.create_table(tname, t.limit(10))
        self.temp_tables.append(tname)

        vname = util.guid()
        self.con.create_view(vname, t.limit(10))
        self.temp_views.append(vname)

        t2 = self.db[tname]
        t2.drop()
        assert tname not in self.db

        t3 = self.db[vname]
        t3.drop()
        assert vname not in self.db


class TestAlterTable(ImpalaE2E, unittest.TestCase):

    def test_rename_table(self):
        tmp_db = '__ibis_tmp_{0}'.format(util.guid()[:4])
        self.con.create_database(tmp_db)
        self.temp_databases.append(tmp_db)

        self.con.create_table('tmp_rename_test',
                              self.con.table('tpch_region'))
        table = self.con.table('tmp_rename_test')

        new_name = 'rename_test'
        table.rename(new_name, database=tmp_db)

        table.execute()

        t = self.con.table(new_name, database=tmp_db)
        assert_equal(table, t)


class TestQueryHDFSData(ImpalaE2E, unittest.TestCase):

    def test_cleanup_tmp_table_on_gc(self):
        hdfs_path = pjoin(self.test_data_dir, 'parquet/tpch_region')
        table = self.con.parquet_file(hdfs_path)
        name = table.op().name
        table = None
        gc.collect()
        _assert_table_not_exists(self.con, name)

    def test_persist_parquet_file_with_name(self):
        hdfs_path = pjoin(self.test_data_dir, 'parquet/tpch_region')

        name = _random_table_name()
        schema = ibis.schema([('r_regionkey', 'int16'),
                              ('r_name', 'string'),
                              ('r_comment', 'string')])
        self.con.parquet_file(hdfs_path, schema=schema,
                              name=name,
                              database=self.tmp_db,
                              persist=True)
        gc.collect()

        # table still exists
        self.con.table(name, database=self.tmp_db)

        _ensure_drop(self.con, name, database=self.tmp_db)

    def test_query_avro(self):
        hdfs_path = pjoin(self.test_data_dir, 'avro/tpch_region_avro')

        avro_schema = {
            "fields": [
                {"type": ["int", "null"], "name": "R_REGIONKEY"},
                {"type": ["string", "null"], "name": "R_NAME"},
                {"type": ["string", "null"], "name": "R_COMMENT"}],
            "type": "record",
            "name": "a"
        }

        table = self.con.avro_file(hdfs_path, avro_schema,
                                   database=self.tmp_db)

        name = table.op().name
        assert name.startswith('{0}.'.format(self.tmp_db))

        # table exists
        self.con.table(name)

        expr = table.r_name.value_counts()
        expr.execute()

        assert table.count().execute() == 5

        df = table.execute()
        assert len(df) == 5

    def test_query_parquet_file_with_schema(self):
        hdfs_path = pjoin(self.test_data_dir, 'parquet/tpch_region')

        schema = ibis.schema([('r_regionkey', 'int16'),
                              ('r_name', 'string'),
                              ('r_comment', 'string')])

        table = self.con.parquet_file(hdfs_path, schema=schema)

        name = table.op().name

        # table exists
        self.con.table(name)

        expr = table.r_name.value_counts()
        expr.execute()

        assert table.count().execute() == 5

    def test_query_parquet_file_like_table(self):
        hdfs_path = pjoin(self.test_data_dir, 'parquet/tpch_region')

        ex_schema = ibis.schema([('r_regionkey', 'int16'),
                                 ('r_name', 'string'),
                                 ('r_comment', 'string')])

        table = self.con.parquet_file(hdfs_path, like_table='tpch_region')

        assert_equal(table.schema(), ex_schema)

    def test_query_parquet_infer_schema(self):
        hdfs_path = pjoin(self.test_data_dir, 'parquet/tpch_region')
        table = self.con.parquet_file(hdfs_path)

        # NOTE: the actual schema should have an int16, but bc this is being
        # inferred from a parquet file, which has no notion of int16, the
        # inferred schema will have an int32 instead.
        ex_schema = ibis.schema([('r_regionkey', 'int32'),
                                 ('r_name', 'string'),
                                 ('r_comment', 'string')])

        assert_equal(table.schema(), ex_schema)

    def test_create_table_persist_fails_if_called_twice(self):
        tname = util.guid()

        hdfs_path = pjoin(self.test_data_dir, 'parquet/tpch_region')
        self.con.parquet_file(hdfs_path, name=tname, persist=True)
        self.temp_tables.append(tname)

        with self.assertRaises(HS2Error):
            self.con.parquet_file(hdfs_path, name=tname, persist=True)

    def test_create_table_reserved_identifier(self):
        table_name = 'distinct'
        expr = self.con.table('functional_alltypes')
        self.con.create_table(table_name, expr)
        self.temp_tables.append(table_name)

        t = self.con.table(table_name)
        t.limit(10).execute()

    def test_query_text_file_regex(self):
        pass

    def test_query_delimited_file_directory(self):
        hdfs_path = pjoin(self.test_data_dir, 'csv')

        schema = ibis.schema([('foo', 'string'),
                              ('bar', 'double'),
                              ('baz', 'int8')])
        name = 'delimited_table_test1'
        table = self.con.delimited_file(hdfs_path, schema, name=name,
                                        database=self.tmp_db,
                                        delimiter=',')
        try:
            expr = (table
                    [table.bar > 0]
                    .group_by('foo')
                    .aggregate([table.bar.sum().name('sum(bar)'),
                                table.baz.sum().name('mean(baz)')]))
            expr.execute()
        finally:
            self.con.drop_table(name, database=self.tmp_db)

    def test_temp_table_concurrency(self):
        pytest.skip('Cannot get this test to run under pytest')

        from threading import Thread, Lock
        import gc
        nthreads = 4

        hdfs_path = pjoin(self.test_data_dir, 'parquet/tpch_region')

        lock = Lock()

        results = []

        def do_something():
            t = self.con.parquet_file(hdfs_path)

            with lock:
                t.limit(10).execute()
                t = None
                gc.collect()
                results.append(True)

        threads = []
        for i in range(nthreads):
            t = Thread(target=do_something)
            t.start()
            threads.append(t)

        [x.join() for x in threads]

        assert results == [True] * nthreads


def _create_table(table_name, expr, database=None, can_exist=False,
                  format='parquet'):
    ast = build_ast(expr)
    select = ast.queries[0]
    statement = ddl.CTAS(table_name, select,
                         database=database,
                         format=format,
                         can_exist=can_exist)
    return statement


def _get_select(expr):
    ast = build_ast(expr)
    select = ast.queries[0]
    context = ast.context

    return select, context


def _random_table_name():
    table_name = '__ibis_test_' + util.guid()
    return table_name


def _assert_table_not_exists(con, table_name, database=None):
    if database is not None:
        tname = '.'.join((database, table_name))
    else:
        tname = table_name

    try:
        con.table(tname)
    except ImpylaError:
        pass
    except:
        raise


def _ensure_drop(con, table_name, database=None):
    con.drop_table(table_name, database=database, force=True)
    _assert_table_not_exists(con, table_name, database=database)

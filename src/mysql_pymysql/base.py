"""
PyMySQL database backend for Django.

Requires PyMySQL: https://github.com/clelland/pymysql
"""
from .py3 import text_type, long_type, reraise

import re
import sys

try:
    import pymysql as Database
    from pymysql.converters import decoders
    from pymysql.constants import FIELD_TYPE, CLIENT
    backend = 'pymysql'

except ImportError:
    e = sys.exc_info()[1]
    from django.core.exceptions import ImproperlyConfigured
    raise ImproperlyConfigured("Error loading PyMySQL module: %s" % e)

from django.db import utils
from django.db.backends import utils as backend_utils
from django.db.backends.base.operations import BaseDatabaseOperations
from django.db.backends.base.base import BaseDatabaseWrapper

from django.db.backends.signals import connection_created
from django.db.backends.mysql.client import DatabaseClient
from django.db.backends.mysql.creation import DatabaseCreation
from .introspection import DatabaseIntrospection
from django.db.backends.mysql.validation import DatabaseValidation
from django.utils.safestring import SafeBytes, SafeText
from django.utils.timezone import is_aware, is_naive, utc
from django.utils.encoding import force_str, force_text
from .schema import DatabaseSchemaEditor
from .features import DatabaseFeatures                      # isort:skip

from django.utils import six
from django.utils.functional import cached_property

# Raise exceptions for database warnings if DEBUG is on
from django.conf import settings
if settings.DEBUG:
    from warnings import filterwarnings
    filterwarnings("error", category=Database.Warning)

DatabaseError = Database.DatabaseError
IntegrityError = Database.IntegrityError

# PyMySQL raises an InternalError with error 1048 (BAD_NULL) -- here we patch
# the error map to force an IntegrityError instead.
from pymysql.err import error_map
error_map[1048] = IntegrityError

django_conversions = decoders.copy()
# It's impossible to import datetime_or_None directly from MySQLdb.times
datetime_or_None = decoders[FIELD_TYPE.DATETIME]

# As with the MySQLdb adapter, PyMySQL returns TIME columns as timedelta --
# so we add a conversion here
def datetime_or_None_with_timezone_support(obj):
    dt = datetime_or_None(obj)
    # Confirm that dt is naive before overwriting its tzinfo.
    if dt is not None and settings.USE_TZ and is_naive(dt):
        dt = dt.replace(tzinfo=utc)
    return dt

django_conversions.update({
    FIELD_TYPE.TIME: backend_utils.typecast_time,
    FIELD_TYPE.DECIMAL: backend_utils.typecast_decimal,
    FIELD_TYPE.NEWDECIMAL: backend_utils.typecast_decimal,
    FIELD_TYPE.DATETIME: datetime_or_None_with_timezone_support,
})


# -----------------------------------------------------------------------------
# The rest of this file is taken verbatim from django/db/backends/mysql/base/py
# which, unfortunately, cannot be imported directly in an environment without
# MySQLdb installed.
# -----------------------------------------------------------------------------


# This should match the numerical portion of the version numbers (we can treat
# versions like 5.0.24 and 5.0.24a as the same). Based on the list of version
# at http://dev.mysql.com/doc/refman/4.1/en/news.html and
# http://dev.mysql.com/doc/refman/5.0/en/news.html .
server_version_re = re.compile(r'(\d{1,2})\.(\d{1,2})\.(\d{1,2})')

# MySQLdb-1.2.1 and newer automatically makes use of SHOW WARNINGS on
# MySQL-4.1 and newer, so the MysqlDebugWrapper is unnecessary. Since the
# point is to raise Warnings as exceptions, this can be done with the Python
# warning module, and this is setup when the connection is created, and the
# standard util.CursorDebugWrapper can be used. Also, using sql_mode
# TRADITIONAL will automatically cause most warnings to be treated as errors.

class CursorWrapper(object):
    """
    A thin wrapper around MySQLdb's normal cursor class so that we can catch
    particular exception instances and reraise them with the right types.

    Implemented as a wrapper, rather than a subclass, so that we aren't stuck
    to the particular underlying representation returned by Connection.cursor().
    """
    codes_for_integrityerror = (1048,)

    def __init__(self, cursor):
        self.cursor = cursor

    def execute(self, query, args=None):
        try:
            return self.cursor.execute(query, args)
        except Database.IntegrityError:
            e = sys.exc_info()
            reraise(utils.IntegrityError, utils.IntegrityError(*e[1].args), e[2])
        except Database.OperationalError:
            e = sys.exc_info()
            # Map some error codes to IntegrityError, since they seem to be
            # misclassified and Django would prefer the more logical place.
            if e[1].code in self.codes_for_integrityerror:
                reraise(utils.IntegrityError, utils.IntegrityError(*e[1].args), e[2])
            raise
        except Database.DatabaseError:
            e = sys.exc_info()
            reraise(utils.DatabaseError, utils.DatabaseError(*e[1].args), e[2])

    def executemany(self, query, args):
        try:
            return self.cursor.executemany(query, args)
        except Database.IntegrityError:
            e = sys.exc_info()
            reraise(utils.IntegrityError, utils.IntegrityError(*e[1].args), e[2])
        except Database.OperationalError:
            e = sys.exc_info()
            # Map some error codes to IntegrityError, since they seem to be
            # misclassified and Django would prefer the more logical place.
            if e[1].code in self.codes_for_integrityerror:
                reraise(utils.IntegrityError, utils.IntegrityError(*e[1].args), e[2])
            raise
        except Database.DatabaseError:
            e = sys.exc_info()
            reraise(utils.DatabaseError, utils.DatabaseError(*e[1].args), e[2])

    def __getattr__(self, attr):
        if attr in self.__dict__:
            return self.__dict__[attr]
        else:
            return getattr(self.cursor, attr)

    def __iter__(self):
        return iter(self.cursor)

class DatabaseOperations(BaseDatabaseOperations):
    compiler_module = "django.db.backends.mysql.compiler"

    def date_extract_sql(self, lookup_type, field_name):
        # http://dev.mysql.com/doc/mysql/en/date-and-time-functions.html
        if lookup_type == 'week_day':
            # DAYOFWEEK() returns an integer, 1-7, Sunday=1.
            # Note: WEEKDAY() returns 0-6, Monday=0.
            return "DAYOFWEEK(%s)" % field_name
        else:
            return "EXTRACT(%s FROM %s)" % (lookup_type.upper(), field_name)

    def date_trunc_sql(self, lookup_type, field_name):
        fields = ['year', 'month', 'day', 'hour', 'minute', 'second']
        format = ('%%Y-', '%%m', '-%%d', ' %%H:', '%%i', ':%%s') # Use double percents to escape.
        format_def = ('0000-', '01', '-01', ' 00:', '00', ':00')
        try:
            i = fields.index(lookup_type) + 1
        except ValueError:
            sql = field_name
        else:
            format_str = ''.join([f for f in format[:i]] + [f for f in format_def[i:]])
            sql = "CAST(DATE_FORMAT(%s, '%s') AS DATETIME)" % (field_name, format_str)
        return sql

    def date_interval_sql(self, sql, connector, timedelta):
        return "(%s %s INTERVAL '%d 0:0:%d:%d' DAY_MICROSECOND)" % (sql, connector,
                timedelta.days, timedelta.seconds, timedelta.microseconds)

    def drop_foreignkey_sql(self):
        return "DROP FOREIGN KEY"

    def force_no_ordering(self):
        """
        "ORDER BY NULL" prevents MySQL from implicitly ordering by grouped
        columns. If no ordering would otherwise be applied, we don't want any
        implicit sorting going on.
        """
        return ["NULL"]

    def fulltext_search_sql(self, field_name):
        return 'MATCH (%s) AGAINST (%%s IN BOOLEAN MODE)' % field_name

    def last_executed_query(self, cursor, sql, params):
        # With MySQLdb, cursor objects have an (undocumented) "_last_executed"
        # attribute where the exact query sent to the database is saved.
        # See MySQLdb/cursors.py in the source distribution.
        return getattr(cursor, '_last_executed', None)

    def no_limit_value(self):
        # 2**64 - 1, as recommended by the MySQL documentation
        return long_type(18446744073709551615)

    def quote_name(self, name):
        if name.startswith("`") and name.endswith("`"):
            return name # Quoting once is enough.
        return "`%s`" % name

    def random_function_sql(self):
        return 'RAND()'

    def sql_flush(self, style, tables, sequences, allow_cascade=False):
        # NB: The generated SQL below is specific to MySQL
        # 'TRUNCATE x;', 'TRUNCATE y;', 'TRUNCATE z;'... style SQL statements
        # to clear all tables of all data
        if tables:
            sql = ['SET FOREIGN_KEY_CHECKS = 0;']
            for table in tables:
                sql.append('%s %s;' % (style.SQL_KEYWORD('TRUNCATE'), style.SQL_FIELD(self.quote_name(table))))
            sql.append('SET FOREIGN_KEY_CHECKS = 1;')

            # 'ALTER TABLE table AUTO_INCREMENT = 1;'... style SQL statements
            # to reset sequence indices
            sql.extend(["%s %s %s %s %s;" % \
                (style.SQL_KEYWORD('ALTER'),
                 style.SQL_KEYWORD('TABLE'),
                 style.SQL_TABLE(self.quote_name(sequence['table'])),
                 style.SQL_KEYWORD('AUTO_INCREMENT'),
                 style.SQL_FIELD('= 1'),
                ) for sequence in sequences])
            return sql
        else:
            return []

    def value_to_db_datetime(self, value):
        if value is None:
            return None

        # MySQL doesn't support tz-aware datetimes
        if is_aware(value):
            if settings.USE_TZ:
                value = value.astimezone(utc).replace(tzinfo=None)
            else:
                raise ValueError("MySQL backend does not support timezone-aware datetimes when USE_TZ is False.")

        # MySQL doesn't support microseconds
        return text_type(value.replace(microsecond=0))

    def value_to_db_time(self, value):
        if value is None:
            return None

        # MySQL doesn't support tz-aware times
        if is_aware(value):
            raise ValueError("MySQL backend does not support timezone-aware times.")

        # MySQL doesn't support microseconds
        return text_type(value.replace(microsecond=0))

    def year_lookup_bounds(self, value):
        # Again, no microseconds
        first = '%s-01-01 00:00:00'
        second = '%s-12-31 23:59:59.99'
        return [first % value, second % value]

    def max_name_length(self):
        return 64

    def bulk_insert_sql(self, fields, num_values):
        items_sql = "(%s)" % ", ".join(["%s"] * len(fields))
        return "VALUES " + ", ".join([items_sql] * num_values)

class DatabaseWrapper(BaseDatabaseWrapper):
    vendor = 'mysql'
    
    _data_types = {
        'AutoField': 'integer AUTO_INCREMENT',
        'BinaryField': 'longblob',
        'BooleanField': 'bool',
        'CharField': 'varchar(%(max_length)s)',
        'CommaSeparatedIntegerField': 'varchar(%(max_length)s)',
        'DateField': 'date',
        'DateTimeField': 'datetime',
        'DecimalField': 'numeric(%(max_digits)s, %(decimal_places)s)',
        'DurationField': 'bigint',
        'FileField': 'varchar(%(max_length)s)',
        'FilePathField': 'varchar(%(max_length)s)',
        'FloatField': 'double precision',
        'IntegerField': 'integer',
        'BigIntegerField': 'bigint',
        'IPAddressField': 'char(15)',
        'GenericIPAddressField': 'char(39)',
        'NullBooleanField': 'bool',
        'OneToOneField': 'integer',
        'PositiveIntegerField': 'integer UNSIGNED',
        'PositiveSmallIntegerField': 'smallint UNSIGNED',
        'SlugField': 'varchar(%(max_length)s)',
        'SmallIntegerField': 'smallint',
        'TextField': 'longtext',
        'TimeField': 'time',
        'UUIDField': 'char(32)',
    }

    @cached_property
    def data_types(self):
        if self.features.supports_microsecond_precision:
            return dict(self._data_types, DateTimeField='datetime(6)', TimeField='time(6)')
        else:
            return self._data_types

    operators = {
        'exact': '= %s',
        'iexact': 'LIKE %s',
        'contains': 'LIKE BINARY %s',
        'icontains': 'LIKE %s',
        'regex': 'REGEXP BINARY %s',
        'iregex': 'REGEXP %s',
        'gt': '> %s',
        'gte': '>= %s',
        'lt': '< %s',
        'lte': '<= %s',
        'startswith': 'LIKE BINARY %s',
        'endswith': 'LIKE BINARY %s',
        'istartswith': 'LIKE %s',
        'iendswith': 'LIKE %s',
    }

#
#	Imported from native mysql module of 1.6
#		
    def _set_autocommit(self, autocommit):
        self.connection.autocommit(autocommit)


    def init_connection_state(self):
        cursor = self.connection.cursor()
        # SQL_AUTO_IS_NULL in MySQL controls whether an AUTO_INCREMENT column
        # on a recently-inserted row will return when the field is tested for
        # NULL.  Disabling this value brings this aspect of MySQL in line with
        # SQL standards.
        cursor.execute('SET SQL_AUTO_IS_NULL = 0')
        cursor.close()

    def get_new_connection(self, conn_params):
        conn = Database.connect(**conn_params)
        conn.encoders[SafeText] = conn.encoders[six.text_type]
        conn.encoders[SafeBytes] = conn.encoders[bytes]
        return conn

    def schema_editor(self, *args, **kwargs):
        "Returns a new instance of this backend's SchemaEditor"
        return DatabaseSchemaEditor(self, *args, **kwargs)

    def get_connection_params(self):
        kwargs = {
            'conv': django_conversions,
            'charset': 'utf8',
        }
        if six.PY2:
            kwargs['use_unicode'] = True
        settings_dict = self.settings_dict
        if settings_dict['USER']:
            kwargs['user'] = settings_dict['USER']
        if settings_dict['NAME']:
            kwargs['db'] = settings_dict['NAME']
        if settings_dict['PASSWORD']:
            kwargs['passwd'] = force_str(settings_dict['PASSWORD'])
        if settings_dict['HOST'].startswith('/'):
            kwargs['unix_socket'] = settings_dict['HOST']
        elif settings_dict['HOST']:
            kwargs['host'] = settings_dict['HOST']
        if settings_dict['PORT']:
            kwargs['port'] = int(settings_dict['PORT'])
        # We need the number of potentially affected rows after an
        # "UPDATE", not the number of changed rows.
        kwargs['client_flag'] = CLIENT.FOUND_ROWS
        kwargs.update(settings_dict['OPTIONS'])
        return kwargs
#
#	EOI
#
		
	
    def __init__(self, *args, **kwargs):
        super(DatabaseWrapper, self).__init__(*args, **kwargs)

        self.server_version = None
        self.features = DatabaseFeatures(self)
        self.ops = DatabaseOperations(self)
        self.client = DatabaseClient(self)
        self.creation = DatabaseCreation(self)
        self.introspection = DatabaseIntrospection(self)
        self.validation = DatabaseValidation(self)

        self.Database = Database


    def _valid_connection(self):
        if self.connection is not None:
            try:
                self.connection.ping()
                return True
            except DatabaseError:
                self.connection.close()
                self.connection = None
        return False

    def _cursor(self):
        new_connection = False
        if not self._valid_connection():
            new_connection = True
            kwargs = {
                'conv': django_conversions,
                'charset': 'utf8',
                'use_unicode': True,
            }
            settings_dict = self.settings_dict
            if settings_dict['USER']:
                kwargs['user'] = settings_dict['USER']
            if settings_dict['NAME']:
                kwargs['db'] = settings_dict['NAME']
            if settings_dict['PASSWORD']:
                kwargs['passwd'] = settings_dict['PASSWORD']
            if settings_dict['HOST'].startswith('/'):
                kwargs['unix_socket'] = settings_dict['HOST']
            elif settings_dict['HOST']:
                kwargs['host'] = settings_dict['HOST']
            if settings_dict['PORT']:
                kwargs['port'] = int(settings_dict['PORT'])
            # We need the number of potentially affected rows after an
            # "UPDATE", not the number of changed rows.
            kwargs['client_flag'] = CLIENT.FOUND_ROWS
            kwargs.update(settings_dict['OPTIONS'])
            kwargs['autocommit'] = self.autocommit = settings_dict.get('AUTOCOMMIT', True)
            if 'timeout' in kwargs:
                kwargs['connect_timeout'] = int(kwargs.pop('timeout'))

            self.connection = Database.connect(**kwargs)

            self.connection.encoders[SafeText] = self.connection.encoders[text_type]
            self.connection.encoders[SafeBytes] = self.connection.encoders[str]
            connection_created.send(sender=self.__class__, connection=self)
        cursor = self.connection.cursor()
        if new_connection:
            # SQL_AUTO_IS_NULL in MySQL controls whether an AUTO_INCREMENT column
            # on a recently-inserted row will return when the field is tested for
            # NULL.  Disabling this value brings this aspect of MySQL in line with
            # SQL standards.
            cursor.execute('SET SQL_AUTO_IS_NULL = 0')
        return CursorWrapper(cursor)

    def _rollback(self):
        try:
            BaseDatabaseWrapper._rollback(self)
        except Database.NotSupportedError:
            pass

    def get_server_version(self):
        if not self.server_version:
            if not self._valid_connection():
                self.cursor()
            m = server_version_re.match(self.connection.get_server_info())
            if not m:
                raise Exception('Unable to determine MySQL version from version string %r' % self.connection.get_server_info())
            self.server_version = tuple([int(x) for x in m.groups()])
        return self.server_version

    def disable_constraint_checking(self):
        """
        Disables foreign key checks, primarily for use in adding rows with forward references. Always returns True,
        to indicate constraint checks need to be re-enabled.
        """
        self.cursor().execute('SET foreign_key_checks=0')
        return True

    def enable_constraint_checking(self):
        """
        Re-enable foreign key checks after they have been disabled.
        """
        self.cursor().execute('SET foreign_key_checks=1')

    def check_constraints(self, table_names=None):
        """
        Checks each table name in `table_names` for rows with invalid foreign key references. This method is
        intended to be used in conjunction with `disable_constraint_checking()` and `enable_constraint_checking()`, to
        determine if rows with invalid references were entered while constraint checks were off.

        Raises an IntegrityError on the first invalid foreign key reference encountered (if any) and provides
        detailed information about the invalid reference in the error message.

        Backends can override this method if they can more directly apply constraint checking (e.g. via "SET CONSTRAINTS
        ALL IMMEDIATE")
        """
        cursor = self.cursor()
        if table_names is None:
            table_names = self.introspection.get_table_list(cursor)
        for table_name in table_names:
            primary_key_column_name = self.introspection.get_primary_key_column(cursor, table_name)
            if not primary_key_column_name:
                continue
            key_columns = self.introspection.get_key_columns(cursor, table_name)
            for column_name, referenced_table_name, referenced_column_name in key_columns:
                cursor.execute("""
                    SELECT REFERRING.`%s`, REFERRING.`%s` FROM `%s` as REFERRING
                    LEFT JOIN `%s` as REFERRED
                    ON (REFERRING.`%s` = REFERRED.`%s`)
                    WHERE REFERRING.`%s` IS NOT NULL AND REFERRED.`%s` IS NULL"""
                    % (primary_key_column_name, column_name, table_name, referenced_table_name,
                    column_name, referenced_column_name, column_name, referenced_column_name))
                for bad_row in cursor.fetchall():
                    raise utils.IntegrityError("The row in table '%s' with primary key '%s' has an invalid "
                        "foreign key: %s.%s contains a value '%s' that does not have a corresponding value in %s.%s."
                        % (table_name, bad_row[0],
                        table_name, column_name, bad_row[1],
                        referenced_table_name, referenced_column_name))

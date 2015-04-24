from django.db.backends.base.features import BaseDatabaseFeatures

class DatabaseFeatures(BaseDatabaseFeatures):
    empty_fetchmany_value = ()
    update_can_self_select = False
    allows_group_by_pk = True
    related_fields_match_type = True
    allow_sliced_subqueries = False
    has_bulk_insert = True
    has_select_for_update = True
    has_select_for_update_nowait = False
    supports_forward_references = False
    supports_regex_backreferencing = False
    supports_date_lookup_using_string = False
    can_introspect_autofield = True
    can_introspect_binary_field = False
    can_introspect_small_integer_field = True
    supports_timezones = False
    requires_explicit_null_ordering_when_grouping = True
    allows_auto_pk_0 = False
    uses_savepoints = True
    can_release_savepoints = True
    atomic_transactions = False
    supports_column_check_constraints = False
    supports_microsecond_precision = False

    def _can_introspect_foreign_keys(self):
        "Confirm support for introspected foreign keys"
        cursor = self.connection.cursor()
        cursor.execute('CREATE TABLE INTROSPECT_TEST (X INT)')
        # This command is MySQL specific; the second column
        # will tell you the default table type of the created
        # table. Since all Django's test tables will have the same
        # table type, that's enough to evaluate the feature.
        cursor.execute("SHOW TABLE STATUS WHERE Name='INTROSPECT_TEST'")
        result = cursor.fetchone()
        cursor.execute('DROP TABLE INTROSPECT_TEST')
        return result[1] != 'MyISAM'


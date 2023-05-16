import logging
import re
from copy import deepcopy
from functools import cached_property, reduce
from operator import and_
from typing import NamedTuple

import sqlglot
import sqlglot.expressions as exp
from input_column import InputColumn, remove_quotes_from_identifiers
from misc import ensure_is_list

logger = logging.getLogger(__name__)
def remove_suffix(c):
    return re.sub("_[l|r]{1}$", "", c)


class InvalidCols(NamedTuple):
    """
    A simple NamedTuple to aid in the construction of
    our log strings.

    It takes in two arguments:
        invalid_type (str): The type of invalid column
            detected. This can be one of invalid_cols,
            invalid_table_pref or invalid_col_suffix.
        invalid_columns (list): A list of the invalid
            columns that have been detected.
    """

    invalid_type: str
    invalid_columns: list

    @property
    def is_valid(self):
        # Quick check to see whether invalid cols exist.
        # Makes list comprehension simpler.
        return True if len(self.invalid_columns) > 0 else False

    @property
    def plural(self):
        return "s" if len(self.invalid_columns) > 1 else ""

    @property
    def columns_as_text(self):
        return ", ".join(f"`{c}`" for c in self.invalid_columns)

    @property
    def invalid_cols(self):
        return (
            f"The following column(s) are missing from one or more "
            f"of your input dataframe(s):\n{self.columns_as_text}"
        )

    @property
    def invalid_table_pref(self):
        return (
            "The following column reference(s) contain invalid "
            "table prefixes (only `l.` and `r.` are valid):"
            f"\n{self.columns_as_text}"
        )

    @property
    def invalid_col_suffix(self):
        return (
            "The following column reference(s) contain invalid "
            "table suffixes (only `_l` and `_r` are valid)"
            f"\n{self.columns_as_text}"
        )

    @property
    def construct_log_string(self):
        if self.invalid_columns:
            # calls invalid_cols, invalid_table_pref, etc
            return getattr(self, self.invalid_type)


class SettingsValidator:
    def __init__(self, linker):
        self.linker = linker
        self.validation_dict = {}

        if linker._settings_obj:
            self._sql_dialect = linker._settings_obj._sql_dialect
        else:
            self._sql_dialect = None

        # Extract additional_columns_to_retain field from settings obj
        self.cols_to_retain = self.clean_list_of_column_names(
            linker._settings_obj._additional_columns_to_retain
        )  # only check if len(n) > 0
        uid_as_tree = InputColumn(linker._settings_obj._unique_id_column_name)
        self.uid = self.clean_list_of_column_names(uid_as_tree)

        self.blocking_rules = linker._settings_dict[
            "blocking_rules_to_generate_predictions"
        ]

    @cached_property
    def input_columns_by_df(self):
        """A dictionary containing all input dataframes and the columns located
        within.

        Returns:
            dict: A dictionary of the format `{"table_name": [col1, col2, ...]}
        """
        # For each input dataframe, grab the column names and create a dictionary
        # of the form: {table_name: [column_1, column_2, ...]}
        input_columns = {
            k: self.clean_list_of_column_names(v.columns)
            for k, v in self.linker._input_tables_dict.items()
        }

        return input_columns

    @cached_property
    def input_columns(self):
        """
        Returns:
            set: The set intersection of all columns contained within each dataset.
        """
        return reduce(and_, self.input_columns_by_df.values())

    def clean_list_of_column_names(self, col_list, as_tree=True):
        """Clean a list of columns names by removing the quote characters
        that may exist.

        Args:
            col_list (list): A list of columns names.
            as_tree (bool): Whether the input columns are already SQL
                trees or need conversions.

        Returns:
            set: A set of column names without quotes.
        """
        col_list = ensure_is_list(col_list)
        if as_tree:
            col_list = [c.input_name_as_tree for c in col_list]
        return set(remove_quotes_from_identifiers(tree).sql() for tree in col_list)

    def remove_prefix_and_suffix_from_column(self, col_syntax_tree):
        col_syntax_tree.args["table"] = None
        return remove_suffix(col_syntax_tree.sql())

    def check_column_exists(self, column_name):
        """Check whether a column name exists within all of the input
        dataframes.

        Args:
            column_name (str): A column name to check

        Returns:
            bool: True if the column exists.
        """
        return column_name in self.input_columns

    def return_missing_columns(self, cols_to_check: set) -> list:
        """
        Args:
            cols_to_check (set): A list of columns to check for the
            existence of.

        Returns:
            list: Returns a list of all input columns not found within
            your raw input tables.
        """
        missing_cols = [
            col for col in cols_to_check if not self.check_column_exists(col)
        ]
        return InvalidCols("invalid_cols", missing_cols)

    def clean_and_return_missing_columns(self, cols):
        cols = set(self.remove_prefix_and_suffix_from_column(c) for c in cols)
        return self.return_missing_columns(cols)

    def validate_table_name(self, col: sqlglot.expressions):
        table_name = col.table
        # If the table name exists, check it's valid.
        return table_name not in ["l", "r"]

    # def validate_table_names(self, cols: list(sqlglot.expressions)):
    def validate_table_names(self, cols):
        # the key to use when producing our error logs
        invalid_type = "invalid_table_pref"
        # list of valid columns
        invalid_cols = [c.sql() for c in cols if self.validate_table_name(c)]
        return InvalidCols(invalid_type, invalid_cols)

    def validate_columns_in_sql_string(
        self,
        sql_string,
        checks,
    ):
        try:
            syntax_tree = sqlglot.parse_one(sql_string, read=self._sql_dialect)
        except Exception:
            # Usually for the `ELSE` clause. If we can't parse a
            # SQL condition, it's better to just pass.
            return

        cols = [
            remove_quotes_from_identifiers(col)
            for col in syntax_tree.find_all(exp.Column)
        ]
        # deepcopy to ensure our syntax trees aren't manipulated
        invalid_tree = [check(deepcopy(cols)) for check in checks]
        # Report only those checks with valid flags (i.e. there's an invalid
        # column in one of the checks)
        invalid_tree = [tree for tree in invalid_tree if tree.is_valid]

        return invalid_tree

    def validate_settings_column(self, settings_id, cols: set):
        missing_cols = self.return_missing_columns(cols)
        if missing_cols.is_valid:
            return settings_id, missing_cols

    @property
    def validate_uid(self):
        return self.validate_settings_column(
            settings_id="unique_id_column_name",
            cols=self.uid,
        )

    @property
    def validate_cols_to_retain(self):
        return self.validate_settings_column(
            settings_id="additional_columns_to_retain",
            cols=self.cols_to_retain,
        )

    @property
    def validate_blocking_rules(self):
        invalid_col_tracker = {}

        for br in self.blocking_rules:
            invalid_br_cols = self.validate_columns_in_sql_string(
                br,
                checks=(
                    self.clean_and_return_missing_columns,
                    self.validate_table_names,
                ),
            )
            if invalid_br_cols:
                invalid_col_tracker.update({br: invalid_br_cols})

        return invalid_col_tracker


class InvalidSettingsLogger(SettingsValidator):
    bold = "\033[1m"
    end = "\033[0m"
    underline = "\033[4m"

    def __init__(self, linker):
        self.settings_validator = super().__init__(linker)
        self.bold_underline = self.bold + self.underline

    def construct_generic_settings_log_string(self, constructor_dict):
        settings_id, InvCols = constructor_dict
        logger.warning(
            f"{self.bold}A problem was found within your setting "
            f"`{settings_id}`:{self.end}\n{InvCols.construct_log_string}"
            "\n"
        )

    def construct_blocking_rule_log_strings(self, invalid_brs):
        log_intro = (
            f"{self.bold}The following blocking rule(s) were "
            f"found to contain invalid column(s):{self.end}\n"
        )

        br_strings = []
        for br, invalid_cols in invalid_brs.items():
            invalid_strings = "\n".join(c.construct_log_string for c in invalid_cols)
            br = f"{self.bold_underline}`{br}`{self.end}"
            br_strings.append(f"{br}:\n{invalid_strings}")

        br_strings = "\n".join(br_strings)
        logger.warning(f"{log_intro}{br_strings}\n")

    def construct_output_logs(self):
        if self.validate_uid:
            self.construct_generic_settings_log_string(self.validate_uid)

        if self.validate_cols_to_retain:
            self.construct_generic_settings_log_string(self.validate_cols_to_retain)

        invalid_brs = self.validate_blocking_rules
        if invalid_brs:
            self.construct_blocking_rule_log_strings(invalid_brs)

        logger.warning(
            "You may want to verify your settings dictionary has "
            "valid inputs in all fields before continuing."
        )

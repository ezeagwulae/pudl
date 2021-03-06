"""A module with functions for loading the pudl database tables."""

import shutil
import os
import io
import contextlib
import pandas as pd
import postgres_copy
import pudl
import pudl.models.entities
import pudl.constants as pc


def _csv_dump_load(df, table_name, engine, csvdir='', keep_csv=False):
    """
    Write a dataframe to CSV and load it into postgresql using COPY FROM.

    The fastest way to load a bunch of records is using the database's native
    text file copy function.  This function dumps a given dataframe out to a
    CSV file, and then loads it into the specified table using a sqlalchemy
    wrapper around the postgresql COPY FROM command, called postgres_copy.

    Note that this creates an additional in-memory representation of the data,
    which takes slightly less memory than the DataFrame itself.

    Args:
        df (pandas.DataFrame): The DataFrame which is to be dumped to CSV and
            loaded into the database. All DataFrame columns must have exactly
            the same names as the database fields they are meant to populate,
            and all column data types must be directly compatible with the
            database fields they are meant to populate. Do any cleanup before
            you call this function.
        table_name (str): The exact name of the database table which the
            DataFrame df is going to be used to populate. It will be used both
            to look up an SQLAlchemy table object in the PUDLBase metadata
            object, and to name the CSV file.
        engine (sqlalchemy.engine): SQLAlchemy database engine, which will be
            used to pull the CSV output into the database.
        csvdir (str): Path to the directory into which the CSV file should be
            saved, if it's being kept.
        keep_csv (bool): True if the CSV output should be saved after the data
            has been loaded into the database. False if they should be deleted.
            NOTE: If multiple COPYs are done for the same table_name, only
            the last will be retained by keep_csv, which may be unsatisfying.
    Returns: Nothing.
    """

    tbl = pudl.models.entities.PUDLBase.metadata.tables[table_name]
    with io.StringIO() as f:
        df.to_csv(f, index=False)
        f.seek(0)
        postgres_copy.copy_from(f, tbl, engine, columns=tuple(df.columns),
                                format='csv', header=True, delimiter=',')
        if keep_csv:
            print(f"DEBUG: writing CSV")
            f.seek(0)
            outfile = os.path.join(csvdir, table_name + '.csv')
            shutil.copyfileobj(f, outfile)


def _fix_int_cols(table_to_fix,
                  transformed_dct,
                  need_fix_inting=pc.need_fix_inting,
                  verbose=True):
    """
    Run fix_int_na on multiple columns per table.

    There are some tables that have one table that needs fix_int_naing, while
    some tables have a few columns.

    Args:
        table_to_fix: the name of the table that needs fixing.
        transformed_dct: dictionary of tables with transformed dfs.
        need_fix_inting: dictionary of tables with columns that need fixing.
    """
    for column in need_fix_inting[table_to_fix]:
        if verbose:
            print("        fixing {} column".format(column))
        transformed_dct[table_to_fix][column] = \
            pudl.helpers.fix_int_na(
                transformed_dct[table_to_fix][column])


class BulkCopy(contextlib.AbstractContextManager):
    """Accumulate several DataFrames, then COPY FROM python to postgresql

    NOTE: You shoud use this class to load one table at a time. To load
    different tables, use different instances of BulkCopy.

    Args:
        table_name (str): The exact name of the database table which the
            DataFrame df is going to be used to populate. It will be used both
            to look up an SQLAlchemy table object in the PUDLBase metadata
            object, and to name the CSV file.
        engine (sqlalchemy.engine): SQLAlchemy database engine, which will be
            used to pull the CSV output into the database.
        buffer (int): Size of data to accumulate (in bytes) before actually
            writing the data into postgresql. (Approximate, because we don't
            introspect memory usage 'deeply'). Default 1 GB.
        csvdir (str): Path to the directory into which the CSV file should be
            saved, if it's being kept.
        keep_csv (bool): True if the CSV output should be saved after the data
            has been loaded into the database. False if they should be deleted.
            NOTE: If multiple COPYs are done for the same table_name, only
            the last will be retained by keep_csv, which may be unsatisfying.
    Example:
    with BulkCopy(my_table, my_engine) as p:
        for df in df_generator:
            p.add(df)
    """

    def __init__(self, table_name, engine, buffer=1024**3,
                 csvdir='', keep_csv=False):
        self.table_name = table_name
        self.engine = engine
        self.buffer = buffer
        self.keep_csv = keep_csv
        self.csvdir = csvdir
        # Initialize a list to keep the dataframes
        self.accumulated_dfs = []
        self.accumulated_size = 0

    def add(self, df):
        """Add a DataFrame to the accumulated list"""
        assert isinstance(df, pd.DataFrame)
        df = self._fix_inting(df)
        # Note: append to a list here, then do a concat when we spill
        self.accumulated_dfs.append(df)
        self.accumulated_size += sum(df.memory_usage())
        if self.accumulated_size > self.buffer:
            # Debugging:
            # print(f"DEBUG: Copying {len(self.accumulated_dfs)} accumulated dataframes, " +
            #       f"totalling {round(self.accumulated_size / 1024**2)} MB")
            self.spill()

    def _check_names(self):
        expected_colnames = set(self.accumulated_dfs[0].columns.values)
        for df in self.accumulated_dfs:
            colnames = set(df.columns.values)
            assert colnames == expected_colnames, (
                "Column names weren't " +
                "constant. BulkCopy should only be used with one table at " +
                "a time, and all columns should be present. Symmetric " +
                "difference between actual and expected:\n" +
                str(colnames.symmetric_difference(
                    expected_colnames))
            )

    def _fix_inting(self, df):
        """Fix integers for columns with NA. See pudl.helpers.fix_int_na"""
        try:
            for column in pc.need_fix_inting[self.table_name]:
                df[column] = pudl.helpers.fix_int_na(df[column])
        except KeyError:
            pass
        return df

    def spill(self):
        """Spill the accumulated dataframes into postgresql"""
        if self.accumulated_dfs:
            self._check_names()
            all_dfs = pd.concat(self.accumulated_dfs,
                                copy=False, ignore_index=True, sort=False)
            _csv_dump_load(all_dfs, table_name=self.table_name, engine=self.engine,
                           csvdir=self.csvdir, keep_csv=self.keep_csv)
        self.accumulated_dfs = []
        self.accumulated_size = 0

    def close(self):
        self.spill()

    def __exit__(self, exception_type, exception_value, traceback):
        self.close()


def dict_dump_load(transformed_dfs,
                   data_source,
                   pudl_engine,
                   need_fix_inting=pc.need_fix_inting,
                   verbose=True,
                   csvdir='',
                   keep_csv=False):
    """
    Wrapper for _csv_dump_load for each data source.
    """
    if verbose:
        print("Loading tables from {} into PUDL:".format(data_source))
    for table_name, df in transformed_dfs.items():
        if verbose and table_name != "hourly_emissions_epacems":
            print("    {}...".format(table_name))
        if table_name in list(need_fix_inting.keys()):
            _fix_int_cols(table_name,
                          transformed_dfs,
                          need_fix_inting=pc.need_fix_inting,
                          verbose=verbose)
        _csv_dump_load(df,
                       table_name,
                       pudl_engine,
                       csvdir=csvdir,
                       keep_csv=keep_csv)

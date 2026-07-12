"""
description_retriever.py

Loads column descriptions from Bird-style database_description CSV files and
enriches an MSchema object with those descriptions during schema construction.

Each CSV in the directory corresponds to one table (filename without extension
is the table name).  The expected columns are:
    original_column_name, column_name, column_description,
    data_format, value_description
"""

import csv
import os
from typing import Dict, Optional, Tuple

from .m_schema import MSchema


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_comment(column_description: str, value_description: str) -> str:
    """Combine column_description and value_description into a single comment."""
    parts = []
    cd = column_description.strip() if column_description else ""
    vd = value_description.strip() if value_description else ""
    
    if cd:
        parts.append(cd)
    if vd and vd != cd:
        parts.append(vd)
    return "  |  ".join(parts)


# ---------------------------------------------------------------------------
# DescriptionRetriever
# ---------------------------------------------------------------------------

class DescriptionRetriever:
    """
    Loads Bird-style column description CSVs and provides lookup by
    (table_name, original_column_name).

    Usage::

        retriever = DescriptionRetriever("/path/to/database_description")
        comment = retriever.get_comment("frpm", "CDSCode")
    """

    def __init__(self, description_dir: str):
        """
        Parameters
        ----------
        description_dir : str
            Directory that contains one CSV file per table
            (e.g.  frpm.csv, schools.csv, satscores.csv).
        """
        self._description_dir = description_dir
        # {table_name_lower: {col_name_lower: {"comment": ..., "data_format": ...}}}
        self._data: Dict[str, Dict[str, Dict]] = {}
        self._load_all()

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def _load_csv(self, table_name: str, csv_path: str) -> None:
        """Parse one description CSV and store entries keyed by lower-cased
        original_column_name."""
        table_key = table_name.lower()
        self._data[table_key] = {}

        with open(csv_path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                # Strip whitespace from both keys and values
                row = {(k.strip() if k else k): (v.strip() if v else "") for k, v in row.items()}

                orig_col = row.get("original_column_name", "").strip()
                if not orig_col:
                    continue

                col_key = orig_col.lower()
                comment = _build_comment(
                    row.get("column_description", ""),
                    row.get("value_description", ""),
                )
                self._data[table_key][col_key] = {
                    "original_column_name": orig_col,
                    "column_name": row.get("column_name", ""),
                    "column_description": row.get("column_description", ""),
                    "data_format": row.get("data_format", ""),
                    "value_description": row.get("value_description", ""),
                    "comment": comment,
                }

    def _load_all(self) -> None:
        """Load every .csv file in *description_dir*."""
        if not os.path.isdir(self._description_dir):
            raise FileNotFoundError(
                f"database_description directory not found: {self._description_dir}"
            )
        for fname in os.listdir(self._description_dir):
            if fname.lower().endswith(".csv"):
                table_name = fname[:-4]  # strip .csv
                csv_path = os.path.join(self._description_dir, fname)
                self._load_csv(table_name, csv_path)

    # ------------------------------------------------------------------
    # Public query API
    # ------------------------------------------------------------------

    def get_row(self, table_name: str, column_name: str) -> Optional[Dict]:
        """Return the full description row for *table_name*.*column_name*, or
        None if not found."""
        table_key = table_name.lower()
        col_key = column_name.lower().strip()
        return self._data.get(table_key, {}).get(col_key)

    def get_comment(self, table_name: str, column_name: str) -> str:
        """Return the combined comment string for a column, or '' if unknown."""
        row = self.get_row(table_name, column_name)
        return row["comment"] if row else ""

    def get_table_columns(self, table_name: str) -> Dict[str, Dict]:
        """Return the full column-description dict for *table_name* (may be
        empty if the table has no description file)."""
        return self._data.get(table_name.lower(), {})

    @property
    def tables(self):
        """Return the set of table names that have description files."""
        return set(self._data.keys())


# ---------------------------------------------------------------------------
# Enrichment helper
# ---------------------------------------------------------------------------

def enrich_mschema_with_descriptions(
    mschema: MSchema,
    retriever: DescriptionRetriever,
    overwrite: bool = False,
) -> MSchema:
    """
    Walk every table/column in *mschema* and fill the ``comment`` field with
    the text from *retriever* when the column matches an entry in the
    database_description CSVs.

    Parameters
    ----------
    mschema : MSchema
        The schema object to enrich (modified **in-place**).
    retriever : DescriptionRetriever
        Pre-loaded retriever for the target database.
    overwrite : bool
        If False (default) skip columns that already have a non-empty comment.
        If True overwrite existing comments.

    Returns
    -------
    MSchema
        The same *mschema* object (mutated in-place, returned for convenience).
    """
    for table_name, table_info in mschema.tables.items():
        for field_name, field_info in table_info["fields"].items():
            existing_comment = field_info.get("comment", "")
            if existing_comment and not overwrite:
                continue

            comment = retriever.get_comment(table_name, field_name)
            if comment:
                field_info["comment"] = comment

    return mschema


# ---------------------------------------------------------------------------
# Convenience: build an enriched MSchema from a SchemaEngine in one call
# ---------------------------------------------------------------------------

def build_enriched_mschema(
    schema_engine,
    description_dir: str,
    overwrite: bool = False,
) -> MSchema:
    """
    Retrieve the MSchema from *schema_engine*, then overlay column descriptions
    from *description_dir*.

    Parameters
    ----------
    schema_engine : SchemaEngine
        An already-initialised SchemaEngine instance whose ``mschema`` property
        holds the base schema.
    description_dir : str
        Path to the Bird-style ``database_description`` directory.
    overwrite : bool
        Passed through to :func:`enrich_mschema_with_descriptions`.

    Returns
    -------
    MSchema
        The enriched MSchema (same object as ``schema_engine.mschema``).

    Example
    -------
    ::

        from sqlalchemy import create_engine
        from mschema.schema_engine import SchemaEngine
        from mschema.description_retriever import build_enriched_mschema

        engine = create_engine("sqlite:///california_schools.db")
        se = SchemaEngine(engine, db_name="california_schools")
        mschema = build_enriched_mschema(
            se,
            description_dir="/path/to/database_description",
        )
        print(mschema.to_mschema())
    """
    retriever = DescriptionRetriever(description_dir)
    return enrich_mschema_with_descriptions(schema_engine.mschema, retriever, overwrite)

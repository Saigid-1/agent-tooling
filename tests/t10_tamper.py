"""Discovery and tampering of projections through SQLite introspection only.

The T10 TEST arm may not read the FEATURE arm. What counts as "the
projection" is therefore discovered from ``sqlite_master``: every table, and
every column of a base table, that the base writer (recorded in the goldens'
``base_schema``) does not create. Virtual tables are compared through the
virtual table, never through their shadow tables.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

GOLDENS = Path(__file__).resolve().parent / 'fixtures' / 't10-read-path-projection' / 'goldens.json'


def base_schema():
    return json.loads(GOLDENS.read_text())['base_schema']


# T12b R4 (tests/t12b_ruled_exceptions.py; Verification, meet): STORE tables a later writer adds that are not part of
# the projection. ``index_outbox`` is the store's queue to the one indexer (T12b B1): a fresh store holds rows for
# its inserts, an upgraded store's outbox starts empty, so its rows are never projection content. Its schema is
# compared exactly instead (store_table_schema).
STORE_TABLES = ('index_outbox',)


def store_table_schema(path: Path):
    """The STORE_TABLES' DDL, the triggers that write them, and ``user_version`` (T12b R4)."""
    with closing(sqlite3.connect(path)) as db:
        tables = dict(db.execute("SELECT name,sql FROM sqlite_master WHERE type='table' AND name IN (SELECT value "
                                 "FROM json_each(?)) ORDER BY name", (json.dumps(STORE_TABLES),)).fetchall())
        triggers = {name: (table, sql) for name, table, sql in db.execute(
            "SELECT name,tbl_name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name")
            if any(table_name in sql for table_name in STORE_TABLES)}
        version = db.execute('PRAGMA user_version').fetchone()[0]
    return {'tables': tables, 'triggers': triggers, 'user_version': version}


def _tables(db):
    rows = db.execute("SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
    virtual = {name for name, sql in rows if sql and sql.upper().startswith('CREATE VIRTUAL TABLE')}
    shadow = {name for name, _ in rows for v in virtual if name != v and name.startswith(v + '_')}
    return [name for name, _ in rows if name not in shadow], virtual


def _columns(db, table):
    return [row[1] for row in db.execute(f'PRAGMA table_info("{table}")')]


def projection_columns(path: Path, schema=None):
    """``{table: [columns]}`` that are not part of the base schema."""
    schema = schema or base_schema()
    result = {}
    with closing(sqlite3.connect(path)) as db:
        tables, _ = _tables(db)
        for table in tables:
            if table in STORE_TABLES:
                continue  # a store table, not projection (T12b R4)
            columns = _columns(db, table)
            base = schema['tables'].get(table)
            extra = columns if base is None else [c for c in columns if c not in base['columns']]
            if extra:
                result[table] = extra
    return result


def _value(value):
    return ('blob', value.hex()) if isinstance(value, bytes) else (type(value).__name__, value)


def projection_dump(path: Path, schema=None):
    """Projection content: new tables (rows sorted) and new columns of base tables (by rowid)."""
    schema = schema or base_schema()
    dump = {}
    with closing(sqlite3.connect(path)) as db:
        for table, columns in projection_columns(path, schema).items():
            quoted = ','.join(f'"{c}"' for c in columns)
            if table in schema['tables']:
                rows = db.execute(f'SELECT rowid,{quoted} FROM "{table}" ORDER BY rowid').fetchall()
            else:
                rows = sorted(db.execute(f'SELECT {quoted} FROM "{table}"').fetchall(),
                              key=lambda r: json.dumps([_value(v) for v in r]))
            dump[table] = {'columns': columns, 'rows': [[_value(v) for v in row] for row in rows]}
    return dump


def replace_text(path: Path, old: str, new: str, *, only=None):
    """Replace ``old`` by ``new`` in every text/blob value of the given ``{table: [columns]}``.

    ``only=None`` means every column of every table (used for the index file).
    Returns the number of values changed.
    """
    changed = 0
    with closing(sqlite3.connect(path)) as db:
        tables, virtual = _tables(db)
        targets = only if only is not None else {t: _columns(db, t) for t in tables}
        for table, columns in targets.items():
            for column in columns:
                sql = (f'UPDATE "{table}" SET "{column}" = CASE typeof("{column}") '
                       f"WHEN 'blob' THEN CAST(replace(CAST(\"{column}\" AS TEXT), ?, ?) AS BLOB) "
                       f'ELSE replace("{column}", ?, ?) END '
                       f"WHERE typeof(\"{column}\") IN ('text','blob') AND instr(CAST(\"{column}\" AS TEXT), ?) > 0")
                try:
                    changed += db.execute(sql, (old, new, old, new, old)).rowcount
                except sqlite3.Error:
                    continue  # e.g. a generated column; nothing a writer could store there
        db.commit()
    return changed


def tamper_claim(path: Path, session_id: str):
    """Change the sealed bytes of every claim of one session so their IDs no longer verify."""
    with closing(sqlite3.connect(path)) as db:
        rows = db.execute('SELECT rowid,payload FROM session_claims WHERE session_id=?', (session_id,)).fetchall()
        for rowid, payload in rows:
            value = json.loads(payload)
            value['asserted_by'] = value['asserted_by'] + ':tampered'
            raw = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()
            db.execute('UPDATE session_claims SET payload=? WHERE rowid=?', (raw, rowid))
        db.commit()
    return len(rows)


def tamper_session(path: Path, session_id: str):
    """Change the sealed bytes of one source session so its ID no longer verifies."""
    with closing(sqlite3.connect(path)) as db:
        payload = db.execute('SELECT payload FROM source_sessions WHERE id=?', (session_id,)).fetchone()[0]
        value = json.loads(payload)
        value['runtime'] = value['runtime'] + '-tampered'
        raw = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()
        changed = db.execute('UPDATE source_sessions SET payload=? WHERE id=?', (raw, session_id)).rowcount
        db.commit()
    return changed

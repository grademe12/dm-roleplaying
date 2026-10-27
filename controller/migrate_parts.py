#!/usr/bin/env python3
"""Offline, explicit migration of the legacy three-part dm_jobs constraint."""
import argparse
import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from common import ControlError, require


LEGACY_CHECK = re.compile(r'CHECK\s*\(\s*part_index\s+BETWEEN\s+0\s+AND\s+2\s*\)', re.I)
CURRENT_CHECK = re.compile(r'CHECK\s*\(\s*part_index\s*>=\s*0\s*\)', re.I)


def migrate(db_path, backup_path):
    """Run with all writers stopped. Preserve rows, indexes, triggers and child FKs.

    Rebuild the parent table with foreign keys temporarily disabled on this connection,
    as required by SQLite's generalized ALTER TABLE procedure. No writable_schema use.
    """
    path, backup = Path(db_path).resolve(), Path(backup_path).resolve()
    require(path.is_file(), 'database_not_initialized')
    require(path != backup, 'backup_path_exists')
    db = sqlite3.connect(path.as_uri()+'?mode=rw', uri=True, isolation_level=None, timeout=5)
    try:
        row = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='dm_jobs'").fetchone()
        require(row is not None, 'missing_dm_jobs')
        sql = row[0]
        if not LEGACY_CHECK.search(sql):
            require(CURRENT_CHECK.search(sql) is not None, 'unsupported_dm_jobs_schema')
            require(not db.execute('PRAGMA foreign_key_check').fetchall(), 'foreign_key_check_failed')
            return {'ok': True, 'reason': 'already_migrated'}
        require(len(LEGACY_CHECK.findall(sql)) == 1, 'unsupported_dm_jobs_schema')
        require(not backup.exists(), 'backup_path_exists')
        rebuilt, count = re.subn(r'^CREATE\s+TABLE\s+["`\[]?dm_jobs["`\]]?',
                                'CREATE TABLE dm_jobs_parts_migration', sql, count=1, flags=re.I)
        require(count == 1, 'unsupported_dm_jobs_schema')
        rebuilt = LEGACY_CHECK.sub('CHECK(part_index >= 0)', rebuilt)
        # Exclusive file creation prevents accidentally overwriting a previous backup.
        with backup.open('xb'):
            pass
        try:
            with closing(sqlite3.connect(backup)) as saved:
                db.backup(saved)
                require(saved.execute('PRAGMA integrity_check').fetchone()[0] == 'ok', 'backup_integrity_failed')
        except Exception:
            backup.unlink()
            raise
        db.execute('PRAGMA foreign_keys=OFF')
        db.execute('BEGIN IMMEDIATE')
        # Re-read under the write lock in case another migration finished first.
        require(db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='dm_jobs'").fetchone()[0] == sql,
                'schema_changed_retry')
        require(not db.execute('PRAGMA foreign_key_check').fetchall(), 'foreign_key_check_failed')
        require(db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok', 'database_integrity_failed')
        objects = db.execute("SELECT sql FROM sqlite_master WHERE tbl_name='dm_jobs' "
                             "AND type IN ('index','trigger') AND sql IS NOT NULL").fetchall()
        columns = [row[1] for row in db.execute('PRAGMA table_info(dm_jobs)')]
        names = ','.join('"'+name.replace('"', '""')+'"' for name in columns)
        before = db.execute(f'SELECT {names} FROM dm_jobs ORDER BY session_id,id').fetchall()
        db.execute(rebuilt)
        db.execute(f'INSERT INTO dm_jobs_parts_migration ({names}) SELECT {names} FROM dm_jobs')
        db.execute('DROP TABLE dm_jobs')
        db.execute('ALTER TABLE dm_jobs_parts_migration RENAME TO dm_jobs')
        for (statement,) in objects:
            db.execute(statement)
        require(db.execute(f'SELECT {names} FROM dm_jobs ORDER BY session_id,id').fetchall() == before,
                'migration_data_mismatch')
        require(not db.execute('PRAGMA foreign_key_check').fetchall(), 'foreign_key_check_failed')
        require(db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok', 'database_integrity_failed')
        db.commit()
        db.execute('PRAGMA foreign_keys=ON')
        return {'ok': True, 'reason': 'migrated', 'preserved_jobs': len(before), 'backup': str(backup)}
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True)
    parser.add_argument('--backup', required=True)
    args = parser.parse_args()
    try:
        result = migrate(args.db, args.backup)
    except (ControlError, sqlite3.Error, OSError) as exc:
        print(json.dumps({'ok': False, 'reason': exc.reason if isinstance(exc, ControlError) else type(exc).__name__}))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

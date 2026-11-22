from pathlib import Path
from scripts.bootstrap_pa_accounts import ARCHIVE, ROOT


def test_pa_bootstrap_uses_bundled_archive():
    assert ARCHIVE == ROOT / 'strategy_archive_16'
    assert (ARCHIVE / 'manifest.json').is_file()


def test_bootstrap_eight_archived_accounts_in_disposable_database(tmp_path, monkeypatch):
    import sqlite3
    from scripts import bootstrap_pa_accounts as bootstrap
    monkeypatch.setattr(bootstrap, 'DB', tmp_path / 'strategy-pool.sqlite3')
    monkeypatch.setattr(bootstrap, 'backup_database', lambda: tmp_path / 'unused-backup.sqlite3')
    bootstrap.main()
    with sqlite3.connect(bootstrap.DB) as db:
        assert db.execute('select count(*) from strategies').fetchone()[0] == 8
        assert db.execute('select count(*) from fills').fetchone()[0] == 0

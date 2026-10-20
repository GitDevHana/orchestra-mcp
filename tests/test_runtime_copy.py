import os
from pathlib import Path
import pytest
from orchestrator.branches import copy_tree, inventory
from orchestrator.audit import snapshot
from orchestrator.server import InstanceLock


def test_live_records_are_omitted_without_reading_locked_file(tmp_path):
    source=tmp_path/'source'
    records=source/'demo-data'/'records'
    source.mkdir()
    (source/'app.py').write_text('print(1)')
    lock=InstanceLock(records)
    try:
        (records/'runs.sqlite3').touch()
        # On Windows the one-byte lock prevents reading this live file.
        if os.name=='nt':
            with pytest.raises(OSError):
                (records/'server.lock').read_bytes()
        result=copy_tree(source,tmp_path/'copy')
        assert 'demo-data/records/' in result['omitted']
        assert (tmp_path/'copy'/'app.py').exists()
        assert not (tmp_path/'copy'/'demo-data'/'records').exists()
        assert inventory(source)==inventory(tmp_path/'copy')
        assert set(snapshot(str(source))['files'])=={'app.py'}
        with pytest.raises(ValueError,match='records directory'):
            copy_tree(records,tmp_path/'wrong')
    finally:
        lock.close()


def test_real_source_copy_error_names_file(tmp_path,monkeypatch):
    source=tmp_path/'source';source.mkdir()
    (source/'important.py').write_text('source')
    def denied(*args):raise PermissionError('locked')
    monkeypatch.setattr('orchestrator.branches.shutil.copy2',denied)
    with pytest.raises(OSError,match='important.py'):
        copy_tree(source,tmp_path/'copy')

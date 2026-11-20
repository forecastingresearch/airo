"""Keep the operator's rollback state intact when keyless verification fails."""
from contextlib import nullcontext
import importlib.util
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


spec = importlib.util.spec_from_file_location(
    'keyless_staging', Path(__file__).parents[1] / 'infra/keyless_staging.py')
keyless = importlib.util.module_from_spec(spec)
spec.loader.exec_module(keyless)


def link(target, path):
    pending = path.with_name(path.name + '.tmp')
    pending.symlink_to(target)
    pending.replace(path)


def write_json(path, value):
    path.write_text(json.dumps(value) + '\n')


@pytest.fixture
def staged_publication(tmp_path, monkeypatch):
    fixture = tmp_path / 'fixture'
    site = fixture / 'forecast/site'
    site.mkdir(parents=True)
    (site / 'index.html').write_text('Keyless fixture forecast')
    runtime_root = tmp_path / 'runtime'
    releases = runtime_root / 'releases'
    original, previous = (releases / name for name in ('original', 'previous'))
    code = tmp_path / 'code'
    code.mkdir()
    for release in (original, previous):
        (release / 'site').mkdir(parents=True)
        (release / 'site/index.html').write_text(release.name)
        (release / 'code').symlink_to(code)
        write_json(release / 'release.json', {'commit': 'a' * 40, 'release': release.name})
    link(original, runtime_root / 'current')
    old_previous = 'releases/previous'
    link(old_previous, runtime_root / 'previous')
    receipt = runtime_root / 'last-deploy.json'
    old_receipt = b'{"release": "original", "previous": "previous"}\n'
    receipt.write_bytes(old_receipt)
    config = tmp_path / 'rsi.json'
    write_json(config, {'root': str(runtime_root), 'public_url': 'https://rsi.forecast.test/'})

    class Runtime:
        root = runtime_root
        current = runtime_root / 'current'
        dashboard = SimpleNamespace(downloads=())
        fail_restore = False

        def __init__(self, config, config_path):
            pass

        def lock(self):
            return nullcontext()

        def release_metadata(self, release):
            return json.loads((release / 'release.json').read_text())

        def verify_site(self, candidate):
            assert (candidate / 'index.html').is_file()

        def activate(self, release):
            if release == original and self.fail_restore:
                raise RuntimeError('The restore publisher failed')
            prior = self.current.resolve(strict=True)
            link(release, self.current)
            if release != prior:
                link(prior, self.root / 'previous')
            write_json(receipt, {'release': release.name, 'previous': prior.name})

    checks = Mock(side_effect=[
        {'result': 'verified', 'checks': {'index.html': 'verified'}},
        {'result': 'unverifiable', 'checks': {'index.html': 'timeout'}},
    ])
    production = SimpleNamespace(
        Runtime=Runtime, data_snapshot=lambda *_: {'sha256': 'fixture-snapshot'},
        link=link, write_json=write_json)
    monkeypatch.setitem(sys.modules, 'production', production)
    monkeypatch.setitem(sys.modules, 'eeai_operator', SimpleNamespace(check_public=checks))
    monkeypatch.setattr(sys, 'path', list(sys.path))
    return SimpleNamespace(fixture=fixture, config=config, code=code, runtime=Runtime,
                           original=original, previous=previous, receipt=receipt,
                           old_receipt=old_receipt, old_previous=old_previous,
                           releases=releases, checks=checks)


def test_failed_public_restore_verification_preserves_original_rollback_state(staged_publication):
    stage = staged_publication
    with pytest.raises(RuntimeError, match='original staging publication did not restore'):
        keyless.publish(stage.fixture, stage.code, stage.config)

    assert stage.runtime.current.resolve(strict=True) == stage.original
    assert os.readlink(stage.runtime.root / 'previous') == stage.old_previous
    assert stage.receipt.read_bytes() == stage.old_receipt
    assert sorted(path.name for path in stage.releases.iterdir()) == ['original', 'previous']
    assert stage.checks.call_count == 2


def test_failed_restore_activation_retains_the_candidate_for_recovery(staged_publication):
    stage = staged_publication
    stage.runtime.fail_restore = True
    with pytest.raises(RuntimeError, match='restore publisher failed'):
        keyless.publish(stage.fixture, stage.code, stage.config)

    candidate = stage.runtime.current.resolve(strict=True)
    assert candidate.name.startswith('keyless-')
    assert candidate.is_dir()
    assert (candidate / 'code').resolve(strict=True) == stage.code
    assert (candidate / 'site').resolve(strict=True) == stage.fixture / 'forecast/site'
    assert (stage.runtime.root / 'previous').resolve(strict=True) == stage.original
    assert json.loads(stage.receipt.read_text())['release'] == candidate.name
    assert stage.checks.call_count == 1

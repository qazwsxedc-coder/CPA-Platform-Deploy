"""Exactly three durable executor integration flows; all data is synthetic."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('upgrader', str(Path(__file__).parents[1] / 'upgrader.py'))
up = importlib.util.module_from_spec(spec)
spec.loader.exec_module(up)
OLD = 'sha256:' + 'a' * 64
NEW = 'sha256:' + 'b' * 64


class Interrupted(BaseException):
    pass


class Runtime:
    def __init__(self, root, fail=False, interrupt=False):
        self.root, self.image, self.running = root, OLD, True
        self.fail, self.interrupt, self.switches = fail, interrupt, 0
        (root / 'history.txt').write_text('historical record')

    def current(self, component):
        return {'imageId': self.image, 'version': 'v1.0.1-custom.1' if self.image == NEW else 'v1.0.0-custom.1',
                'running': self.running, 'containerId': self.image, 'healthy': self.running}

    def preflight(self, component, release):
        return {}

    def stop(self, component):
        self.running = False

    def start(self, component):
        self.running = True

    def backup(self, job):
        assert not self.running
        target = self.root / 'backup'
        target.mkdir()
        (target / 'history.txt').write_text((self.root / 'history.txt').read_text())
        return str(target)

    def install(self, component, image):
        self.image, self.running = image, True
        self.switches += 1
        if self.interrupt:
            self.interrupt = False
            raise Interrupted()

    def check(self, component, image, release, job):
        assert self.running and self.image == image
        if self.fail and image == NEW:
            raise up.Fault('startup_failed')


class Flows(unittest.TestCase):
    def fixture(self, root, **kwargs):
        runtime = Runtime(root, **kwargs)
        executor = up.Executor(root / 'upgrades', runtime)
        executor.initialize()
        release = {'releaseId': 'manager-v1.0.1-custom.1', 'component': 'manager',
                   'version': 'v1.0.1-custom.1', 'imageId': NEW, 'allowedFromImageIds': [OLD],
                   'migrationMode': 'none', 'migrationRequired': False, 'rollbackDataCompatible': True}
        up.atomic_json(executor.root / 'catalog/releases.json', {'schemaVersion': 1, 'releases': [release]})
        request = executor.enqueue(release)
        return executor, runtime, request

    def test_upgrade_success(self):
        with tempfile.TemporaryDirectory() as directory:
            ex, runtime, request = self.fixture(Path(directory))
            ex.process()
            status = up.read_json(ex.root / ('status/' + request['id'] + '.json'))
            self.assertEqual(status['state'], 'succeeded')
            self.assertEqual(runtime.image, NEW)
            self.assertEqual((Path(status['backupPath']) / 'history.txt').read_text(), 'historical record')
            self.assertFalse((ex.root / 'requests/active.json').exists())
            self.assertEqual(ex.last_result()['id'], request['id'])

    def test_failure_rolls_back_image_preserves_data(self):
        with tempfile.TemporaryDirectory() as directory:
            ex, runtime, request = self.fixture(Path(directory), fail=True)
            ex.process()
            self.assertEqual(ex.last_result()['state'], 'rolled_back')
            self.assertEqual(runtime.image, OLD)
            self.assertEqual((Path(directory) / 'history.txt').read_text(), 'historical record')
            self.assertIn(request['releaseId'], ex.failed_releases())

    def test_restart_reconciles_without_repeating_switch(self):
        with tempfile.TemporaryDirectory() as directory:
            ex, runtime, request = self.fixture(Path(directory), interrupt=True)
            with self.assertRaises(Interrupted):
                ex.process()
            self.assertEqual(up.read_json(ex.root / ('journal/' + request['id'] + '.json'))['state'], 'installing')
            restarted = up.Executor(ex.root, runtime)
            restarted.process()
            self.assertEqual(restarted.last_result()['state'], 'succeeded')
            self.assertEqual(runtime.switches, 1)
            restarted.process()
            self.assertEqual(runtime.switches, 1)


if __name__ == '__main__':
    unittest.main()

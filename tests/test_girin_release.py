"""Offline checks for the release source, using the verified build payloads."""
from pathlib import Path
import json
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys._MEIPASS = str(ROOT)
import api
import app_meta
import modrinth_api
import preflight


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.mods = self.root / 'mods'
        self.mods.mkdir()
        self.mocks = [patch.object(preflight, '_config_path', return_value=str(self.root / 'config.json')),
                      patch.object(preflight, 'write_log', lambda message: None),
                      patch.object(api.Api, 'game_running', lambda self: {'running': False}),
                      patch.object(api, '_gr_game_active', lambda self: False)]
        for mock in self.mocks:
            mock.start()
        self.addCleanup(lambda: [mock.stop() for mock in reversed(self.mocks)])
        self.addCleanup(self.temp.cleanup)
        self.a = api.Api()
        self.a._mods_dir = str(self.mods)
        self.a._registry = modrinth_api.ModRegistry(str(self.mods))
        self.a._policy_applied = True

    def install_all(self):
        for mid in api._GR_ITEMS:
            self.assertTrue(self.a.install_mod(mid)['ok'])

    def fake(self, filename, mid, version):
        with zipfile.ZipFile(self.mods / filename, 'w') as jar:
            jar.writestr('fabric.mod.json', json.dumps({'id': api._GR_ITEMS[mid]['fabric_id'], 'version': version}))

    def test_release_metadata_and_default_connect(self):
        self.assertEqual('2.7.2', app_meta.APP_VERSION)
        self.assertEqual('giraffemc.com', app_meta.SERVER_HOST)
        self.assertTrue(self.a.get_game_connection()['auto_connect'])
        self.assertNotEqual('<lambda>', preflight.check_app_update.__name__)

    def test_no_automatic_install_and_required_gate(self):
        self.assertTrue(self.a.refresh_required_mods()['changed'])
        self.assertFalse(self.a.refresh_required_mods()['changed'])
        self.assertFalse(self.a.can_launch()['ok'])
        self.assertEqual([], list(self.mods.glob('*.jar')))

    def test_manual_install_actual_file_detection_and_remove_guard(self):
        self.install_all()
        self.assertTrue(self.a.can_launch()['ok'])
        for mid in api._GR_ITEMS:
            self.assertEqual('required', self.a.remove_mod(mid)['error'])
        with patch.object(self.a._registry, 'is_installed', return_value=False):
            self.assertTrue(self.a.can_launch()['ok'])

    def test_card_refresh_tracks_file_removal_and_restore(self):
        self.install_all()
        self.a.refresh_required_mods()
        seed = api._GR_INDEX[0]
        path = self.mods / seed['filename']
        data = path.read_bytes()
        path.unlink()
        self.assertEqual('not_installed', self.a.refresh_required_mods()['statuses'][seed['id']])
        self.assertFalse(self.a.can_launch()['ok'])
        path.write_bytes(data)
        self.assertEqual('installed', self.a.refresh_required_mods()['statuses'][seed['id']])

    def test_new_version_preserved_and_old_version_backed_up(self):
        self.install_all()
        seed = api._GR_INDEX[0]
        data = (self.mods / seed['filename']).read_bytes()
        self.fake('newer.jar', seed['id'], '99.0.0')
        result = self.a.refresh_required_mods()
        self.assertEqual('99.0.0', result['versions'][seed['id']]['installed'])
        self.assertFalse((self.mods / seed['filename']).exists())
        self.assertEqual(data, (self.root / 'launcher-backups/girin-required' / seed['filename']).read_bytes())
        self.assertTrue(self.a.can_launch()['ok'])

    def test_game_running_postpones_cleanup(self):
        self.install_all()
        seed = api._GR_INDEX[0]
        self.fake('newer.jar', seed['id'], '99.0.0')
        with patch.object(api, '_gr_game_active', return_value=True):
            self.a.refresh_required_mods()
            self.assertTrue((self.mods / seed['filename']).exists())
        self.a.refresh_required_mods()
        self.assertFalse((self.mods / seed['filename']).exists())

    def test_locked_old_file_is_retained_then_retried(self):
        self.install_all()
        seed = api._GR_INDEX[0]
        self.fake('newer.jar', seed['id'], '99.0.0')
        with patch.object(api.os, 'rename', side_effect=PermissionError('locked')):
            self.a.refresh_required_mods()
            self.assertTrue((self.mods / seed['filename']).exists())
            self.assertFalse(self.a.can_launch()['ok'])
        self.a.refresh_required_mods()
        self.assertFalse((self.mods / seed['filename']).exists())

    def test_equal_version_or_prerelease_duplicates_are_not_guessed(self):
        seed = api._GR_INDEX[0]
        self.fake('one.jar', seed['id'], '1.2.3')
        self.fake('two.jar', seed['id'], '1.2.3+other')
        self.assertFalse(self.a.can_launch()['ok'])
        self.assertEqual(2, len(list(self.mods.glob('*.jar'))))

    def test_connection_save_restart_and_failure_rollback(self):
        self.a._config['theme'] = 'light'
        self.assertTrue(self.a.set_game_connection(False)['ok'])
        self.assertFalse(api.Api().get_game_connection()['auto_connect'])
        self.assertEqual('light', json.loads((self.root / 'config.json').read_text())['theme'])
        self.assertTrue(self.a.set_game_connection(True)['ok'])
        self.assertTrue(api.Api().get_game_connection()['auto_connect'])
        self.assertFalse(self.a.set_game_connection('false')['ok'])
        with patch.object(preflight, 'save_config', lambda config: None):
            self.assertFalse(self.a.set_game_connection(False)['ok'])
            self.assertTrue(self.a.get_game_connection()['auto_connect'])

    def test_launch_uses_persisted_choice(self):
        with patch.object(api, '_gr_original_client_launch', side_effect=lambda obj, quick_connect: quick_connect):
            self.assertTrue(self.a.client_launch())
            self.a._config['girin_auto_connect'] = False
            self.assertFalse(self.a.client_launch())
            self.assertTrue(self.a.client_launch(True))

    def test_actual_command_server_argument(self):
        fake_instance = types.ModuleType('instance')
        fake_instance.installed_version_id = lambda: 'fabric-26.2'
        fake_instance.is_version_ready = lambda version: True
        fake_instance.instance_dir = lambda: str(self.root)
        lib = types.ModuleType('minecraft_launcher_lib')
        lib.command = types.SimpleNamespace(get_minecraft_command=lambda version, path, options: dict(options))
        launcher = types.ModuleType('launcher')
        launcher.__file__ = str(ROOT / 'launcher.py')
        with patch.dict(sys.modules, {'auth': types.ModuleType('auth'), 'instance': fake_instance, 'minecraft_launcher_lib': lib}):
            exec(compile((ROOT / 'launcher.py').read_text(encoding='utf-8'), str(ROOT / 'launcher.py'), 'exec'), launcher.__dict__)
            launcher._java_executable = lambda: 'javaw.exe'
            account = {'mc_username': 'Test', 'mc_uuid': 'test-uuid', 'mc_access_token': 'TEST_ONLY'}
            self.assertEqual('giraffemc.com', launcher.build_command(account, quick_connect=True)['quickPlayMultiplayer'])
            self.assertNotIn('quickPlayMultiplayer', launcher.build_command(account, quick_connect=False))


if __name__ == '__main__':
    unittest.main(verbosity=2)

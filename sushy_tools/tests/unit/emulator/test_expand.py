#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import base64
import copy
import os
import tempfile
from unittest import mock

import bcrypt
from oslotest import base

from sushy_tools.emulator import main


def patch_resource(name):
    def decorator(func):
        return mock.patch.object(main.Application, name,
                                 new_callable=mock.PropertyMock)(func)
    return decorator


def _seed_system_mocks(systems_mock, managers_mock, chassis_mock,
                       indicators_mock):
    """Wire up just enough of the providers to render two systems."""
    sys_inst = systems_mock.return_value
    type(sys_inst).systems = mock.PropertyMock(
        return_value=['host0', 'host1'])
    sys_inst.uuid.return_value = 'zzzz'
    sys_inst.get_power_state.return_value = 'On'
    sys_inst.get_total_memory.return_value = 1
    sys_inst.get_total_cpus.return_value = 2
    sys_inst.get_boot_device.return_value = 'Cd'
    sys_inst.get_boot_mode.return_value = 'Legacy'
    managers_mock.return_value.get_managers_for_system.return_value = ['mgr0']
    chassis_mock.return_value.chassis = ['chassis0']
    indicators_mock.return_value.get_indicator_state.return_value = 'Off'


class ExpandHelpersTestCase(base.BaseTestCase):
    """Unit tests for the $expand helper functions (no HTTP)."""

    TREE = {
        '/redfish/v1/Systems': {
            '@odata.id': '/redfish/v1/Systems',
            'Members': [{'@odata.id': '/redfish/v1/Systems/1'}],
        },
        '/redfish/v1/Systems/1': {
            '@odata.id': '/redfish/v1/Systems/1',
            'Id': '1',
            'Memory': {'@odata.id': '/redfish/v1/Systems/1/Memory'},
            'Links': {
                'Chassis': [{'@odata.id': '/redfish/v1/Chassis/1'}],
            },
        },
        '/redfish/v1/Systems/1/Memory': {
            '@odata.id': '/redfish/v1/Systems/1/Memory',
            'Id': 'Memory',
        },
        '/redfish/v1/Chassis/1': {
            '@odata.id': '/redfish/v1/Chassis/1',
            'Id': 'Chassis1',
        },
    }

    def _fetch(self, odata_id):
        return copy.deepcopy(self.TREE.get(odata_id))

    def _expand(self, src, etype, levels):
        node = copy.deepcopy(self.TREE[src])
        return main._expand_node(node, False, etype, levels, self._fetch)

    def test_parse_expand_variants(self):
        self.assertEqual(('.', 1), main._parse_expand('.'))
        self.assertEqual(('*', 1), main._parse_expand('*'))
        self.assertEqual(('~', 1), main._parse_expand('~'))
        self.assertEqual(('.', 1), main._parse_expand('.($levels=1)'))
        self.assertEqual(('.', 3), main._parse_expand('.($levels=3)'))
        self.assertEqual(('*', 2), main._parse_expand('*($levels=2)'))

    def test_parse_expand_clamps_zero_to_one(self):
        self.assertEqual(('.', 1), main._parse_expand('.($levels=0)'))

    def test_parse_expand_rejects_bad_input(self):
        for bad in ('', None, 'x', '.foo', '.($levels=abc)', '.(levels=1)'):
            self.assertIsNone(main._parse_expand(bad))

    def test_is_redfish_ref(self):
        self.assertTrue(main._is_redfish_ref({'@odata.id': '/x'}))
        self.assertTrue(
            main._is_redfish_ref({'@odata.id': '/x', '@odata.type': 'T'}))
        self.assertFalse(main._is_redfish_ref({'@odata.id': '/x', 'Id': '1'}))
        self.assertFalse(main._is_redfish_ref({'@odata.type': 'T'}))
        self.assertFalse(main._is_redfish_ref({'@odata.id': 123}))
        self.assertFalse(main._is_redfish_ref({}))
        self.assertFalse(main._is_redfish_ref([]))
        self.assertFalse(main._is_redfish_ref('x'))

    def test_dot_expands_subordinate_not_links(self):
        out = self._expand('/redfish/v1/Systems/1', '.', 1)
        self.assertEqual('Memory', out['Memory']['Id'])
        self.assertEqual({'@odata.id': '/redfish/v1/Chassis/1'},
                         out['Links']['Chassis'][0])

    def test_tilde_expands_links_only(self):
        out = self._expand('/redfish/v1/Systems/1', '~', 1)
        self.assertEqual({'@odata.id': '/redfish/v1/Systems/1/Memory'},
                         out['Memory'])
        self.assertEqual('Chassis1', out['Links']['Chassis'][0]['Id'])

    def test_star_expands_both(self):
        out = self._expand('/redfish/v1/Systems/1', '*', 1)
        self.assertEqual('Memory', out['Memory']['Id'])
        self.assertEqual('Chassis1', out['Links']['Chassis'][0]['Id'])

    def test_levels_one_does_not_recurse(self):
        out = self._expand('/redfish/v1/Systems', '.', 1)
        self.assertEqual('1', out['Members'][0]['Id'])
        self.assertEqual({'@odata.id': '/redfish/v1/Systems/1/Memory'},
                         out['Members'][0]['Memory'])

    def test_levels_two_recurses(self):
        out = self._expand('/redfish/v1/Systems', '.', 2)
        self.assertEqual('Memory', out['Members'][0]['Memory']['Id'])

    def test_expand_max_levels_default(self):
        with mock.patch.dict(os.environ):
            os.environ.pop('SUSHY_EMULATOR_EXPAND_MAX_LEVELS', None)
            main.app.config.pop('SUSHY_EMULATOR_EXPAND_MAX_LEVELS', None)
            self.assertEqual(5, main._expand_max_levels())

    def test_expand_max_levels_from_config(self):
        main.app.config['SUSHY_EMULATOR_EXPAND_MAX_LEVELS'] = 3
        self.addCleanup(main.app.config.pop,
                        'SUSHY_EMULATOR_EXPAND_MAX_LEVELS', None)
        self.assertEqual(3, main._expand_max_levels())

    def test_expand_max_levels_from_env(self):
        main.app.config.pop('SUSHY_EMULATOR_EXPAND_MAX_LEVELS', None)
        with mock.patch.dict(
                os.environ, {'SUSHY_EMULATOR_EXPAND_MAX_LEVELS': '8'}):
            self.assertEqual(8, main._expand_max_levels())

    def test_expand_max_levels_invalid_falls_back(self):
        main.app.config.pop('SUSHY_EMULATOR_EXPAND_MAX_LEVELS', None)
        with mock.patch.dict(
                os.environ, {'SUSHY_EMULATOR_EXPAND_MAX_LEVELS': 'nope'}):
            self.assertEqual(5, main._expand_max_levels())


class ExpandRequestTestCase(base.BaseTestCase):
    """Integration: the after_request hook rewrites real responses."""

    def setUp(self):
        super(ExpandRequestTestCase, self).setUp()
        self.app = main.app.test_client()

    @patch_resource('storage')
    @patch_resource('indicators')
    @patch_resource('chassis')
    @patch_resource('managers')
    @patch_resource('systems')
    def test_expand_inlines_members(
            self, systems_mock, managers_mock, chassis_mock,
            indicators_mock, storage_mock):
        _seed_system_mocks(systems_mock, managers_mock, chassis_mock,
                           indicators_mock)

        # Without $expand the collection Members are shallow references.
        plain = self.app.get('/redfish/v1/Systems')
        self.assertEqual({'@odata.id': '/redfish/v1/Systems/host0'},
                         plain.json['Members'][0])

        # With $expand=.($levels=1) each Member is inlined as a full system,
        # proving the after_request hook re-dispatched the sub-fetches.
        resp = self.app.get('/redfish/v1/Systems?$expand=.($levels=1)')
        self.assertEqual(200, resp.status_code)
        members = resp.json['Members']
        self.assertEqual('host0', members[0]['Id'])
        self.assertEqual('host1', members[1]['Id'])

    @patch_resource('systems')
    def test_bad_expand_is_noop(self, systems_mock):
        type(systems_mock.return_value).systems = mock.PropertyMock(
            return_value=['host0'])
        resp = self.app.get('/redfish/v1/Systems?$expand=bogus')
        self.assertEqual(200, resp.status_code)
        self.assertEqual({'@odata.id': '/redfish/v1/Systems/host0'},
                         resp.json['Members'][0])


class ExpandAuthTestCase(base.BaseTestCase):
    """Expansion must keep working behind the WSGI basic-auth middleware.

    The middleware authorizes the outer request then pops the Authorization
    header, so the hook can't replay credentials; it dispatches sub-fetches
    internally instead. This is the path the live deployment exercises.
    """

    def setUp(self):
        super(ExpandAuthTestCase, self).setUp()
        digest = bcrypt.hashpw(b'secret', bcrypt.gensalt()).decode()
        fd, self.auth_file = tempfile.mkstemp(suffix='.htpasswd')
        with os.fdopen(fd, 'w') as handle:
            handle.write('admin:%s\n' % digest)
        self.addCleanup(os.unlink, self.auth_file)
        original = main.app.wsgi_app
        main.app.wsgi_app = main.RedfishAuthMiddleware(
            original, self.auth_file)
        self.addCleanup(setattr, main.app, 'wsgi_app', original)
        self.app = main.app.test_client()
        token = base64.b64encode(b'admin:secret').decode()
        self.auth_headers = {'Authorization': 'Basic ' + token}

    @patch_resource('storage')
    @patch_resource('indicators')
    @patch_resource('chassis')
    @patch_resource('managers')
    @patch_resource('systems')
    def test_expand_inlines_behind_auth(
            self, systems_mock, managers_mock, chassis_mock,
            indicators_mock, storage_mock):
        _seed_system_mocks(systems_mock, managers_mock, chassis_mock,
                           indicators_mock)

        # No credentials -> the middleware rejects the request.
        self.assertEqual(
            401, self.app.get('/redfish/v1/Systems').status_code)

        # Authorized request still expands, even though the middleware strips
        # the Authorization header before the view and the hook ever see it.
        resp = self.app.get('/redfish/v1/Systems?$expand=.($levels=1)',
                            headers=self.auth_headers)
        self.assertEqual(200, resp.status_code)
        self.assertEqual('host0', resp.json['Members'][0]['Id'])

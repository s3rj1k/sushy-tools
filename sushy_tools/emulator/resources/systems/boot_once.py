# Copyright 2026 sushy-tools authors
#
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

"""One-time boot for the libvirt driver.

libvirt has no native one-time boot. Arm a domain with the target device +
<on_reboot>destroy</on_reboot>, revert the persistent config to disk after it
starts, then restart it from disk when the guest reboot stops it (Redfish
BootSourceOverrideEnabled=Once). Disabled unless SUSHY_EMULATOR_BOOT_ONCE is
set; runs in a daemon thread.
"""

import threading

from sushy_tools.emulator import constants

try:
    import libvirt

except ImportError:
    libvirt = None


class BootOnceMonitor(object):
    """Simulate one-time boot via on_reboot=destroy + restart-on-stop."""

    def __init__(self, driver, logger):
        self._driver = driver
        self._logger = logger
        self._uri = driver._uri
        self._armed = {}
        self._lock = threading.Lock()
        self._conn = None
        self._thread = None
        self._running = False

    def _domain_uuid(self, identity):
        return self._driver._get_domain(identity).UUIDString()

    def mark(self, identity):
        """Arm a one-time boot: force destroy-on-reboot for the system."""
        try:
            domain_uuid = self._domain_uuid(identity)

        except Exception as exc:
            self._logger.warning(
                'boot-once: cannot resolve domain "%s": %s', identity, exc)
            return

        with self._lock:
            if domain_uuid in self._armed:
                return

        prior = self._driver._get_on_reboot(identity)
        self._driver._set_on_reboot(identity, 'destroy')
        with self._lock:
            self._armed[domain_uuid] = prior
        self._logger.info('boot-once: armed domain %s', domain_uuid)

    def revert_after_start(self, identity):
        """Revert the persistent config to disk after a power-on."""
        try:
            domain_uuid = self._domain_uuid(identity)

        except Exception:
            return

        with self._lock:
            prior = self._armed.get(domain_uuid)

        if prior is None:
            return

        self._driver.set_boot_device(identity, constants.DEVICE_TYPE_HDD)
        self._driver._set_on_reboot(identity, prior)

    def clear(self, identity):
        """Disarm a one-time boot and restore on_reboot."""
        try:
            domain_uuid = self._domain_uuid(identity)

        except Exception:
            return

        with self._lock:
            prior = self._armed.pop(domain_uuid, None)

        if prior is not None:
            self._driver._set_on_reboot(identity, prior)

    def start(self):
        """Register the lifecycle handler and run the event loop."""
        if libvirt is None:
            self._logger.warning(
                'boot-once: libvirt module unavailable; listener disabled')
            return

        if self._running:
            return

        libvirt.virEventRegisterDefaultImpl()
        self._conn = libvirt.open(self._uri)
        self._conn.domainEventRegisterAny(
            None, libvirt.VIR_DOMAIN_EVENT_ID_LIFECYCLE,
            self._on_lifecycle, None)

        self._running = True
        self._thread = threading.Thread(
            target=self._run, name='boot-once-monitor', daemon=True)
        self._thread.start()
        self._logger.info(
            'boot-once: lifecycle listener started (uri=%s)', self._uri)

    def _run(self):
        while self._running:
            try:
                libvirt.virEventRunDefaultImpl()

            except Exception as exc:
                self._logger.warning('boot-once: event loop error: %s', exc)

    def _on_lifecycle(self, conn, domain, event, detail, opaque):
        if event != libvirt.VIR_DOMAIN_EVENT_STOPPED:
            return

        domain_uuid = domain.UUIDString()
        with self._lock:
            armed = domain_uuid in self._armed

        if not armed:
            return

        # A forced stop (ForceOff -> DESTROYED) must stay down; only a guest
        # reboot/shutdown (on_reboot=destroy) should boot disk. Disarm either
        # way: the one-time Pxe boot already happened at power-on.
        if detail != libvirt.VIR_DOMAIN_EVENT_STOPPED_DESTROYED:
            self._logger.info(
                'boot-once: armed domain %s stopped (detail=%s); restarting',
                domain_uuid, detail)

            try:
                domain.create()

            except Exception as exc:
                self._logger.warning(
                    'boot-once: restart of %s failed: %s', domain_uuid, exc)

        with self._lock:
            self._armed.pop(domain_uuid, None)

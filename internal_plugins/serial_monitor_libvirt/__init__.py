#
# Copyright(c) 2026 Unvertical
# SPDX-License-Identifier: BSD-3-Clause
#

import os
import threading
from datetime import datetime

import libvirt

from core.test_run import TestRun

# libvirt dispatches stream callbacks from an event loop. Its default implementation
# is process-wide, so it is registered once and serves every plugin instance.
_event_loop_lock = threading.Lock()
_event_loop_started = False


def _run_event_loop():
    while True:
        libvirt.virEventRunDefaultImpl()


def _start_event_loop():
    global _event_loop_started
    with _event_loop_lock:
        if _event_loop_started:
            return
        libvirt.virEventRegisterDefaultImpl()
        threading.Thread(target=_run_event_loop, name="libvirt-event-loop", daemon=True).start()
        _event_loop_started = True


class SerialMonitorPlugin:
    def __init__(self, params, config):
        print("Serial Monitor LibVirt Plugin initialization")
        try:
            self.url = config["url"]
            self.vm_name = config["vm_name"]
        except KeyError:
            raise Exception("Missing fields in config! ('url','vm_name' are required fields)")
        # Name of the console device as in the domain XML alias (e.g. "serial0");
        # None selects the first console.
        self.dev_name = config.get("dev_name")
        # Take over the console even if someone else (e.g. 'virsh console') holds it
        self.force = config.get("force", False)
        self.reconnect_interval = config.get("reconnect_interval", 5)

        self.conn = None
        self.domain = None
        self.stream = None
        self.reconnect_timer = None
        self.log_file = None
        self.lock = threading.Lock()
        self.closed = False

    def pre_setup(self):
        print("Serial Monitor LibVirt Plugin pre setup")
        dut_dir = os.path.join(TestRun.LOGGER.base_dir, "dut_info", TestRun.config["host"])
        os.makedirs(dut_dir, exist_ok=True)
        self.log_file = open(os.path.join(dut_dir, "serial.log"), "ab", buffering=0)

        _start_event_loop()
        self.conn = libvirt.open(self.url)
        self.domain = self.conn.lookupByName(self.vm_name)
        if not self.__connect():
            self.__schedule_reconnect()

    def post_setup(self):
        pass

    def teardown(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
        # Callbacks check 'closed', so libvirt cleanup can run without holding the lock
        if self.reconnect_timer is not None:
            libvirt.virEventRemoveTimeout(self.reconnect_timer)
            self.reconnect_timer = None
        self.__close_stream()
        if self.conn is not None:
            try:
                self.conn.close()
            except libvirt.libvirtError:
                pass
        with self.lock:
            self.log_file.close()

    def send_to_serial(self, message):
        """Insert a marker into the collected serial log (it is not sent to the DUT)"""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
        self.__write(f"\n[{timestamp}] {message}\n".encode())

    def __write(self, data):
        with self.lock:
            if not self.log_file.closed:
                self.log_file.write(data)

    def __connect(self):
        flags = libvirt.VIR_DOMAIN_CONSOLE_FORCE if self.force else 0
        try:
            if not self.conn.isAlive():
                self.conn = libvirt.open(self.url)
                self.domain = self.conn.lookupByName(self.vm_name)
            stream = self.conn.newStream(libvirt.VIR_STREAM_NONBLOCK)
            self.domain.openConsole(self.dev_name, stream, flags)
            stream.eventAddCallback(
                libvirt.VIR_STREAM_EVENT_READABLE
                | libvirt.VIR_STREAM_EVENT_ERROR
                | libvirt.VIR_STREAM_EVENT_HANGUP,
                self.__stream_event,
                None,
            )
        except libvirt.libvirtError as e:
            self.send_to_serial(f"<serial monitor: failed to open console: {e}>")
            return False
        self.stream = stream
        self.send_to_serial("<serial monitor: console connected>")
        return True

    def __close_stream(self):
        stream, self.stream = self.stream, None
        if stream is None:
            return
        try:
            stream.eventRemoveCallback()
            stream.abort()
        except libvirt.libvirtError:
            pass

    def __schedule_reconnect(self):
        if self.reconnect_timer is None:
            self.reconnect_timer = libvirt.virEventAddTimeout(
                self.reconnect_interval * 1000, self.__reconnect, None
            )

    def __reconnect(self, timer, opaque):
        if self.closed or not self.__connect():
            return
        libvirt.virEventRemoveTimeout(timer)
        self.reconnect_timer = None

    def __stream_event(self, stream, events, opaque):
        if self.closed:
            return
        if events & libvirt.VIR_STREAM_EVENT_READABLE:
            try:
                data = stream.recv(4096)
            except libvirt.libvirtError:
                data = b""
            if data == -2:
                return
            if data:
                self.__write(data)
                return
        # EOF, error or hangup - console is gone (e.g. domain was destroyed)
        self.send_to_serial("<serial monitor: console disconnected>")
        self.__close_stream()
        self.__schedule_reconnect()


plugin_class = SerialMonitorPlugin

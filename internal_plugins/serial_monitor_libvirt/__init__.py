#
# Copyright(c) 2026 Unvertical
# SPDX-License-Identifier: BSD-3-Clause
#

import os
import re
import signal
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta

import libvirt
import pytest

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
        # Console errors are logged to serial.log, don't spam stderr with them
        libvirt.registerErrorHandler(lambda ctx, error: None, None)
        threading.Thread(target=_run_event_loop, name="libvirt-event-loop", daemon=True).start()
        _event_loop_started = True


DEFAULT_WARNING_PATTERNS = [
    r"WARNING:",
    r"soft lockup",
    r"rcu: INFO: .* detected stalls",
    r"INFO: task .* blocked for more than",
]
DEFAULT_BUG_PATTERNS = [
    r"BUG:",
    r"kernel BUG at",
    r"\bOops:",
    r"general protection fault",
    r"Kernel panic",
]
# Limit of kernel messages reported to test log per test and log level, the rest is only
# in serial.log
MAX_REPORTS = 10

# Bugs detected by the event loop thread, to be raised in the main (test) thread
_pending_bugs = []


def _raise_pending_bugs():
    bugs = []
    while _pending_bugs:
        bugs.append(_pending_bugs.pop(0))
    if bugs:
        pytest.fail("\n".join(bugs))


def _in_runtest_hook(frame):
    while frame is not None:
        code = frame.f_code
        if code.co_name == "from_call" and code.co_filename.endswith(
            os.path.join("_pytest", "runner.py")
        ):
            return True
        frame = frame.f_back
    return False


def _bug_signal_handler(signum, frame):
    # Pytest reports exceptions as test result only inside the runtest hook call; anywhere
    # else (e.g. in report generation) it would break pytest itself. In such case the bug
    # stays pending until the next test step.
    if _in_runtest_hook(frame):
        _raise_pending_bugs()


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
        self.warning_patterns = [
            re.compile(p) for p in config.get("warning_patterns", DEFAULT_WARNING_PATTERNS)
        ]
        self.bug_patterns = [
            re.compile(p) for p in config.get("bug_patterns", DEFAULT_BUG_PATTERNS)
        ]
        # Interrupt the test on bug, otherwise just report it as an error
        self.interrupt_on_bug = config.get("interrupt_on_bug", True)

        self.conn = None
        self.domain = None
        self.stream = None
        self.reconnect_timer = None
        self.log_file = None
        self.lock = threading.Lock()
        self.closed = False
        self.host = None
        self.test_identifier = None
        self.line_buffer = b""
        # (log method, message) waiting to be logged from the main thread
        self.pending_reports = []
        self.reported = {}
        self.bug_found = False
        self.expected_kind = None
        self.expected_pattern = None
        self.expected_seen = threading.Event()
        self.expected_bug_found = False

    def pre_setup(self):
        print("Serial Monitor LibVirt Plugin pre setup")
        self.host = TestRun.config["host"]
        # Test identifier is printed to kernel log and may contain anything
        self.test_identifier = TestRun.LOGGER.unique_test_identifier
        dut_dir = os.path.join(TestRun.LOGGER.base_dir, "dut_info", self.host)
        os.makedirs(dut_dir, exist_ok=True)
        self.log_file = open(os.path.join(dut_dir, "serial.log"), "ab", buffering=0)
        signal.signal(signal.SIGUSR1, _bug_signal_handler)

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
            if self.log_file:
                self.log_file.close()
        # Bug detected too late to interrupt this test must not interrupt the next one
        _pending_bugs.clear()

    def send_to_serial(self, message):
        """
        Insert a marker into the collected serial log (it is not sent to the DUT).
        Called from the main thread on each test step, so also report kernel messages
        detected so far.
        """
        self.__mark(message)
        self.__flush_reports()
        _raise_pending_bugs()

    @contextmanager
    def expect(self, kind, pattern, timeout: timedelta = timedelta(seconds=30)):
        """
        Expect kernel messages of given kind ("warning" or "bug") within the block.
        They are logged as info instead of warning or error, but bugs still interrupt
        the test. Fails if no message matching the pattern arrives within the timeout
        after the block. Once an expected bug occurs, all following bugs in the test are
        considered its aftermath.
        """
        if kind not in ("warning", "bug"):
            raise ValueError(f"Unknown kernel message kind '{kind}'")
        self.expected_seen.clear()
        self.expected_pattern = re.compile(pattern)
        self.expected_kind = kind
        try:
            yield
            if not self.expected_seen.wait(timeout.total_seconds()):
                TestRun.fail(
                    f"Expected kernel {kind} matching '{pattern}' not found "
                    f"on {self.host} serial console"
                )
        finally:
            self.expected_kind = None
            self.__flush_reports()

    def __mark(self, message):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
        self.__write(f"\n[{timestamp}] {message}\n".encode())

    def __flush_reports(self):
        while self.pending_reports:
            log_method, message = self.pending_reports.pop(0)
            reported = self.reported.get(log_method, 0)
            if reported < MAX_REPORTS:
                getattr(TestRun.LOGGER, log_method)(message)
            elif reported == MAX_REPORTS:
                getattr(TestRun.LOGGER, log_method)(
                    f"More kernel messages on {self.host}, see serial.log"
                )
            self.reported[log_method] = reported + 1

    def __scan(self, data):
        lines = (self.line_buffer + data).split(b"\n")
        self.line_buffer = lines.pop()
        if len(self.line_buffer) > 4096:
            lines.append(self.line_buffer)
            self.line_buffer = b""
        for raw_line in lines:
            line = raw_line.decode(errors="replace").strip()
            if self.test_identifier and self.test_identifier in line:
                continue
            if any(p.search(line) for p in self.warning_patterns):
                kind, log_method = "warning", "warning"
            elif any(p.search(line) for p in self.bug_patterns):
                kind, log_method = "bug", "error"
            else:
                continue
            message = f"Kernel {kind} on {self.host}: {line}"
            expected = kind == self.expected_kind
            if expected or (kind == "bug" and self.expected_bug_found):
                log_method = "info"
                message = f"Expected kernel {kind} on {self.host}: {line}"
                if kind == "bug":
                    self.expected_bug_found = True
                if expected and self.expected_pattern.search(line):
                    self.expected_seen.set()
            # Logged also for bugs, so that a test catching the exception still fails
            self.pending_reports.append((log_method, message))
            if kind == "bug":
                self.__interrupt(message)

    def __interrupt(self, message):
        # Only the first bug interrupts the test, the following ones are part of its aftermath
        if not self.interrupt_on_bug or self.bug_found:
            return
        self.bug_found = True
        _pending_bugs.append(message)
        # Signal interrupts also blocking calls, e.g. waiting for output of a hung command
        signal.pthread_kill(threading.main_thread().ident, signal.SIGUSR1)

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
            self.__mark(f"<serial monitor: failed to open console: {e}>")
            return False
        self.stream = stream
        self.__mark("<serial monitor: console connected>")
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
                self.__scan(data)
                return
        # EOF, error or hangup - console is gone (e.g. domain was destroyed)
        self.__mark("<serial monitor: console disconnected>")
        self.__close_stream()
        self.__schedule_reconnect()


plugin_class = SerialMonitorPlugin

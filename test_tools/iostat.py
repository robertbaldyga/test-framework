#
# Copyright(c) 2020 Intel Corporation
# SPDX-License-Identifier: BSD-3-Clause-Clear
#

from core.test_run import TestRun
from storage_devices.device import Device
from test_utils.size import Size, Unit


class IOstat:
    def __init__(self, stat_list: list = None):
        if stat_list is None:
            stat_list = [0, 0, 0, 0, 0]
        if len(stat_list) < 5:
            stat_list.extend([0, 0, 0, 0, 0])
        self.transfers_per_second = float(stat_list[0])
        self.reads_per_second = Size(float(stat_list[1]), Unit.KibiByte)
        self.writes_per_second = Size(float(stat_list[2]), Unit.KibiByte)
        self.total_reads = Size(float(stat_list[3]), Unit.KibiByte)
        self.total_writes = Size(float(stat_list[4]), Unit.KibiByte)

    def __str__(self):
        return (
            f"IO stats:\n"
            f"Transfers per second: {self.transfers_per_second}\n"
            f"Kilobytes read per second: {self.reads_per_second}\n"
            f"Kilobytes written per second: {self.writes_per_second}\n"
            f"Kilobytes read: {self.total_reads}\n"
            f"Kilobytes written: {self.total_writes}\n"
        )

    def __eq__(self, other):
        if not other:
            return False
        return (
            self.transfers_per_second == other.transfers_per_second
            and self.reads_per_second == other.reads_per_second
            and self.writes_per_second == other.writes_per_second
            and self.total_reads == other.total_reads
            and self.total_writes == other.total_writes
        )

    @staticmethod
    def get_iostat(device: Device):
        """Returns object of IOstat class containing basic statistics displayed
           in kibibytes/kibibytes per second."""
        lines = TestRun.executor.run(
            f"iostat -dk {device.system_path} "
            f"| grep {device.system_path.strip('/dev/')}").stdout.splitlines()
        iostat_list = []
        for line in lines:
            args = line.split()
            if args[0] == device.system_path.strip('/dev/'):
                iostat_list = args[1:]
        stats = IOstat(iostat_list)
        return stats

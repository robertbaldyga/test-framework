#
# Copyright(c) 2020 Intel Corporation
# SPDX-License-Identifier: BSD-3-Clause-Clear
#
from time import sleep

from core.test_run_utils import TestRun
from storage_devices.device import Device
from test_utils import os_utils
from test_utils.output import CmdException


class ScsiDebug:
    def __init__(self, params):
        self.params = params
        self.module_name = "scsi_debug"

    def pre_setup(self):
        pass

    def post_setup(self):
        self.teardown()
        sleep(1)
        load_output = os_utils.load_kernel_module(self.module_name, self.params)
        if load_output.exit_code != 0:
            raise CmdException(f"Failed to load {self.module_name} module", load_output)
        TestRun.LOGGER.info(f"{self.module_name} loaded successfully.")
        sleep(10)
        TestRun.scsi_debug_devices = Device.get_scsi_debug_devices()

    def teardown(self):
        unload_output = os_utils.unload_kernel_module(self.module_name)
        if unload_output.exit_code != 0 \
                and "is not currently loaded" not in unload_output.stderr:
            TestRun.LOGGER.info(f"Failed to unload {self.module_name} module\n{unload_output}")


plugin_class = ScsiDebug

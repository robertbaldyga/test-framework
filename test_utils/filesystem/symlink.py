#
# Copyright(c) 2019 Intel Corporation
# SPDX-License-Identifier: BSD-3-Clause-Clear
#
from core.test_run import TestRun
from test_utils.filesystem.file import File


class Symlink(File):
    def __init__(self, full_path, target):
        self.target = target
        File.__init__(self, full_path)

    def md5sum(self, binary=True):
        output = TestRun.executor.run(
            f"md5sum {'-b' if binary else ''} {self.target}")
        if output.exit_code != 0:
            raise Exception(
                f"Md5sum command execution failed! {output.stdout}\n{output.stderr}")
        return output.stdout.split()[0]

#
# Copyright(c) 2019-2020 Intel Corporation
# SPDX-License-Identifier: BSD-3-Clause-Clear
#

from attotime import attotimedelta


class Time(attotimedelta):
    def total_microseconds(self):
        self.total_nanoseconds() / 1000

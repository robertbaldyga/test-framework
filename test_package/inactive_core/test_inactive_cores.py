#
# Copyright(c) 2019 Intel Corporation
# SPDX-License-Identifier: BSD-3-Clause-Clear
#


import pytest
from test_package.test_properties import TestProperties
from test_package.conftest import base_prepare
from storage_devices.disk import DiskType
from test_utils.size import Size, Unit
from cas_configuration.cache_config import CacheMode
from api.cas import casadm
from test_tools.dd import Dd


@pytest.mark.parametrize(
    "prepare_and_cleanup", [{"core_count": 1, "cache_count": 1}], indirect=True
)
def test_core_inactive(prepare_and_cleanup):
    """
        1. Start cache with 3 cores.
        2. Stop cache.
        3. Remove one of core devices.
        4. Load cache.
        5. Check if cache has appropriate number of valid and inactive core devices.
    """
    cache, core_device = prepare()

    cache_device = cache.cache_device
    stats = cache.get_cache_statistics()

    assert stats["core devices"] == 3
    assert stats["inactive core devices"] == 0

    TestProperties.LOGGER.info("Stopping cache")
    cache.stop()

    TestProperties.LOGGER.info("Removing one of core devices")
    core_device.remove_partitions()
    core_device.create_partitions([Size(1, Unit.GibiByte), Size(1, Unit.GibiByte)])

    TestProperties.LOGGER.info("Loading cache with missing core device")
    cache = casadm.start_cache(cache_device, load=True)
    stats = cache.get_cache_statistics()

    assert stats["core devices"] == 3
    assert stats["inactive core devices"] == 1


@pytest.mark.parametrize(
    "prepare_and_cleanup", [{"core_count": 1, "cache_count": 1}], indirect=True
)
def test_core_inactive_stats(prepare_and_cleanup):
    """
        1. Start cache with 3 cores.
        2. Switch cache into WB mode.
        3. Issue IO to each core.
        4. Stop cache without flush.
        5. Remove two core devices.
        6. Load cache.
        7. Check if cache stats are equal to sum of valid and inactive cores stats.
        8. Check if percentage values are calculated properly.
    """
    cache, core_device = prepare()

    cache_device = cache.cache_device

    TestProperties.LOGGER.info(cache_device)
    TestProperties.LOGGER.info("Switching cache mode to WB")
    cache.set_cache_mode(cache_mode=CacheMode.WB)
    cores = cache.get_core_devices()
    TestProperties.LOGGER.info("Issue IO to each core")
    for core in cores:
        dd = (
            Dd()
            .input("/dev/zero")
            .output(core.system_path)
            .count(1000)
            .block_size(Size(4, Unit.KibiByte))
        ).run()

    TestProperties.LOGGER.info("Stopping cache with dirty data")
    cores[2].flush_core()
    cache.stop(no_data_flush=True)

    TestProperties.LOGGER.info("Removing two of core devices")
    core_device.remove_partitions()
    core_device.create_partitions([Size(1, Unit.GibiByte)])

    TestProperties.LOGGER.info("Loading cache with missing core device")
    cache = casadm.start_cache(cache_device, load=True)

    # Accumulate valid cores stats
    cores_occupancy = 0
    cores_clean = 0
    cores_dirty = 0
    cores = cache.get_core_devices()
    for core in cores:
        core_stats = core.get_core_statistics()
        cores_occupancy += core_stats["occupancy"].value
        cores_clean += core_stats["clean"].value
        cores_dirty += core_stats["dirty"].value

    cache_stats = cache.get_cache_statistics()
    # Add inactive core stats
    cores_occupancy += cache_stats["inactive occupancy"].value
    cores_clean += cache_stats["inactive clean"].value
    cores_dirty += cache_stats["inactive dirty"].value

    assert cache_stats["occupancy"].value == cores_occupancy
    assert cache_stats["dirty"].value == cores_dirty
    assert cache_stats["clean"].value == cores_clean

    cache_stats_percentage = cache.get_cache_statistics(percentage_val=True)
    # Calculate expected percentage value of inactive core stats
    inactive_occupancy_perc = (
        cache_stats["inactive occupancy"].value / cache_stats["cache size"].value
    )
    inactive_clean_perc = (
        cache_stats["inactive clean"].value / cache_stats["occupancy"].value
    )
    inactive_dirty_perc = (
        cache_stats["inactive dirty"].value / cache_stats["occupancy"].value
    )

    inactive_occupancy_perc = round(100 * inactive_occupancy_perc, 1)
    inactive_clean_perc = round(100 * inactive_clean_perc, 1)
    inactive_dirty_perc = round(100 * inactive_dirty_perc, 1)

    TestProperties.LOGGER.info(cache_stats_percentage)
    assert inactive_occupancy_perc == cache_stats_percentage["inactive occupancy"]
    assert inactive_clean_perc == cache_stats_percentage["inactive clean"]
    assert inactive_dirty_perc == cache_stats_percentage["inactive dirty"]


def prepare():
    base_prepare()
    cache_device = next(
        disk
        for disk in TestProperties.dut.disks
        if disk.disk_type in [DiskType.optane, DiskType.nand]
    )
    core_device = next(
        disk
        for disk in TestProperties.dut.disks
        if (
            disk.disk_type.value > cache_device.disk_type.value and disk != cache_device
        )
    )

    cache_device.create_partitions([Size(500, Unit.MebiByte)])
    core_device.create_partitions(
        [Size(1, Unit.GibiByte), Size(1, Unit.GibiByte), Size(1, Unit.GibiByte)]
    )

    cache_device = cache_device.partitions[0]
    core_device_1 = core_device.partitions[0]
    core_device_2 = core_device.partitions[1]
    core_device_3 = core_device.partitions[2]

    TestProperties.LOGGER.info("Staring cache")
    cache = casadm.start_cache(cache_device, force=True)
    TestProperties.LOGGER.info("Adding core device")
    core_1 = cache.add_core(core_dev=core_device_1)
    core_2 = cache.add_core(core_dev=core_device_2)
    core_3 = cache.add_core(core_dev=core_device_3)

    return cache, core_device

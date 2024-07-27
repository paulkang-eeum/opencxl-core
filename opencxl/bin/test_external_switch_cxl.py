import asyncio
from opencxl.cxl.environment import parse_cxl_environment
from opencxl.apps.single_logical_device import SingleLogicalDevice
from opencxl.apps.cxl_complex_host import (
    CxlComplexHost,
    CxlComplexHostConfig,
    RootPortClientConfig,
    ROOT_PORT_SWITCH_TYPE,
    RootComplexMemoryControllerConfig,
)
from opencxl.apps.cxl_host import CxlHost
from opencxl.util.logger import logger
from opencxl.util.component import RunnableComponent, LabeledComponent
from opencxl.drivers.pci_bus_driver import PciBusDriver
from opencxl.drivers.cxl_bus_driver import CxlBusDriver
from opencxl.drivers.cxl_mem_driver import CxlMemDriver
from typing import List, cast


class SimpleHostWrapper:
    def __init__(self, host: CxlHost):
        self._host = host

    async def write_config(self, bdf: int, offset: int, size: int, value: int):
        await self._host._root_port_device.write_config(bdf, offset, size, value)

    async def read_config(self, bdf: int, offset: int, size: int) -> int:
        return await self._host._root_port_device.read_config(bdf, offset, size)

    async def write_mmio(self, address: int, size: int, value: int):
        await self._host._root_port_device.write_mmio(address, value, size, False)

    async def read_mmio(self, address: int, size: int) -> int:
        return await self._host._root_port_device.read_mmio(address, size, False)

    async def write_cxl_mem(self, address: int, value: int) -> int:
        return await self._host._root_port_device.cxl_mem_write(address, value)

    async def read_cxl_mem(self, address: int) -> int:
        return await self._host._root_port_device.cxl_mem_read(address)

    def get_hpa_base(self) -> int:
        return self._host._root_port_device.get_hpa_base()

    def get_root_bus(self) -> int:
        return 1

    def get_mmio_base_address(self) -> int:
        return 0x80000000


class TestRunner(LabeledComponent):
    def __init__(self, apps: List[RunnableComponent]):
        super().__init__()
        self._apps = apps

    async def run(self):
        tasks = []
        for app in self._apps:
            tasks.append(asyncio.create_task(app.run()))
        tasks.append(asyncio.create_task(self.run_test()))
        await asyncio.gather(*tasks)

    async def wait_for_ready(self):
        tasks = []
        for app in self._apps:
            tasks.append(asyncio.create_task(app.wait_for_ready()))
        await asyncio.gather(*tasks)

    async def stop_test(self):
        for app in self._apps:
            await app.stop()

    async def run_test(self):
        logger.info(self._create_message("Waiting for Apps to be ready"))
        await self.wait_for_ready()
        host = cast(CxlHost, self._apps[0])
        wrapper = SimpleHostWrapper(host)

        pci_bus_driver = PciBusDriver(wrapper)
        logger.info(self._create_message("Starting PCI bus driver init"))
        await pci_bus_driver.init()
        logger.info(self._create_message("Completed PCI bus driver init"))
        cxl_bus_driver = CxlBusDriver(pci_bus_driver, wrapper)
        logger.info(self._create_message("Starting CXL bus driver init"))
        await cxl_bus_driver.init()
        logger.info(self._create_message("Completed CXL bus driver init"))
        cxl_mem_driver = CxlMemDriver(cxl_bus_driver, wrapper)

        logger.info(self._create_message("Test: Single HDM configuration"))

        logger.info(self._create_message("Resetting all HDM decoders"))
        await cxl_mem_driver.reset_hdm_decoders()
        hpa_base = wrapper.get_hpa_base()
        address_ranges = []
        next_available_hpa_base = hpa_base
        for device in cxl_mem_driver.get_devices():
            size = device.get_memory_size()
            successful = await cxl_mem_driver.attach_single_mem_device(
                device, next_available_hpa_base, size
            )
            if successful:
                address_ranges.append((next_available_hpa_base, size))
                next_available_hpa_base += size
            else:
                raise Exception(
                    f"Failed to attach mem device {device.pci_device_info.get_bdf_string()}"
                )

        for address, size in address_ranges:
            await wrapper.write_cxl_mem(address, address)
            logger.info(self._create_message(f"Wrote 0x{address:X} at 0x{address:X}"))
            read_value = await wrapper.read_cxl_mem(address)
            logger.info(self._create_message(f"Read 0x{read_value:X} at 0x{address:X}"))

        logger.info(self._create_message("Test: Multiple HDM configuration"))

        logger.info(self._create_message("Resetting all HDM decoders"))
        await cxl_mem_driver.reset_hdm_decoders()
        hpa_base = wrapper.get_hpa_base()
        devices = cxl_mem_driver.get_devices()
        size = 0
        for device in devices:
            size += device.get_memory_size()
        interleaving_granularity = 256
        await cxl_mem_driver.attach_multiple_mem_devices(
            devices, hpa_base, size, interleaving_granularity
        )

        access_step = 64
        access_count = 100
        address = hpa_base
        for _ in range(access_count):
            await wrapper.write_cxl_mem(address, address)
            logger.info(self._create_message(f"Wrote 0x{address:X} at 0x{address:X}"))
            read_value = await wrapper.read_cxl_mem(address)
            logger.info(self._create_message(f"Read 0x{read_value:X} at 0x{address:X}"))
            address += access_step

        logger.info(self._create_message("Completed Testing"))
        await self.stop_test()


def main():
    # Set up logger
    log_file = "test.log"
    log_level = "DEBUG"
    show_timestamp = True
    show_loglevel = True
    show_linenumber = True
    logger.create_log_file(
        f"logs/{log_file}",
        loglevel=log_level if log_level else "INFO",
        show_timestamp=show_timestamp,
        show_loglevel=show_loglevel,
        show_linenumber=show_linenumber,
    )

    apps = []

    switch_host = "0.0.0.0"
    switch_port = 8000
    use_complex_host = False

    if use_complex_host:
        host_config = CxlComplexHostConfig(
            host_name="CXLHost",
            root_bus=0,
            root_port_switch_type=ROOT_PORT_SWITCH_TYPE.PASS_THROUGH,
            root_ports=[
                RootPortClientConfig(port_index=0, switch_host=switch_host, switch_port=switch_port)
            ],
            memory_ranges=[],
            memory_controller=RootComplexMemoryControllerConfig(
                memory_size=0x10000, memory_filename="memory_dram.bin"
            ),
        )
        host = CxlComplexHost(host_config)
        apps.append(host)
    else:
        host = CxlHost(
            port_index=0,
            switch_host=switch_host,
            switch_port=switch_port,
            hm_mode=False,
            hdm_init=False,
        )
        apps.append(host)

    # Add PCI devices
    for port in range(1, 5):
        memory_size = 256 * 1024 * 1024
        device = SingleLogicalDevice(port, memory_size=memory_size, memory_file=f"mem{port}.bin")
        apps.append(device)

    test_runner = TestRunner(apps)
    asyncio.run(test_runner.run())


if __name__ == "__main__":
    main()

import asyncio
from opencxl.apps.cxl_complex_host import (
    CxlComplexHost,
    CxlComplexHostConfig,
    RootPortClientConfig,
    ROOT_PORT_SWITCH_TYPE,
    RootComplexMemoryControllerConfig,
)
from opencxl.apps.pci_device import PciDevice
from opencxl.util.logger import logger
from opencxl.util.component import RunnableComponent, LabeledComponent
from opencxl.drivers.pci_bus_driver import PciBusDriver
from typing import List, cast


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

    async def run_test(self):
        logger.info(self._create_message("Waiting for Apps to be ready"))
        await self.wait_for_ready()
        # host = cast(CxlComplexHost, self._apps[0])
        # bus_driver = PciBusDriver(host.get_root_complex())
        # logger.info(self._create_message("Starting PCI bus driver init"))
        # await bus_driver.init()
        # logger.info(self._create_message("Completed PCI bus driver init"))


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

    # Add Host
    # switch_host = "0.0.0.0"
    # switch_port = 8000
    # host_config = CxlComplexHostConfig(
    #     host_name="CXLHost0",
    #     root_bus=0,
    #     root_port_switch_type=ROOT_PORT_SWITCH_TYPE.PASS_THROUGH,
    #     root_ports=[
    #         RootPortClientConfig(port_index=0, switch_host=switch_host, switch_port=switch_port)
    #     ],
    #     memory_ranges=[],
    #     memory_controller=RootComplexMemoryControllerConfig(
    #         memory_size=0x10000, memory_filename="memory_dram.bin"
    #     ),
    # )
    # host = CxlComplexHost(host_config)
    # apps.append(host)

    # Add PCI devices
    for port in range(2, 5):
        device = PciDevice(port, 0x1000)
        apps.append(device)

    test_runner = TestRunner(apps)
    asyncio.run(test_runner.run())


if __name__ == "__main__":
    main()

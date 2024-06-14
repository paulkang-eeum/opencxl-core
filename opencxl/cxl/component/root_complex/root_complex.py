"""
 Copyright (c) 2024, Eeum, Inc.

 This software is licensed under the terms of the Revised BSD License.
 See LICENSE for details.
"""

from typing import Optional, List
from dataclasses import dataclass, field
import asyncio
from opencxl.util.logger import logger
from opencxl.util.component import RunnableComponent
from opencxl.cxl.component.cxl_connection import CxlConnection
from opencxl.cxl.component.root_complex.io_bridge import IoBridge, IoBridgeConfig
from opencxl.cxl.component.root_complex.memory_fifo import MemoryFifoPair
from opencxl.cxl.component.root_complex.root_port_switch import (
    SimpleRootPortSwitch,
    RootPortSwitchConfig,
    RootPortSwitchPortConfig,
    ROOT_PORT_SWITCH_TYPE,
)
from opencxl.cxl.component.root_complex.home_agent import HomeAgent, HomeAgentConfig, MemoryRange
from opencxl.cxl.component.root_complex.memory_controller import (
    MemoryController,
    MemoryControllerConfig,
)
from opencxl.cxl.component.root_complex.cache_coherency_bridge import (
    CacheCoherencyBridge,
    CacheCoherencyBridgeConfig,
)

"""

TODO: Add an internal PCIe switch for routing PCIe packets between root ports

"""


@dataclass
class RootComplexMemoryControllerConfig:
    memory_size: int
    memory_filename: str


@dataclass
class RootComplexConfig:
    root_port_switch_type: ROOT_PORT_SWITCH_TYPE
    host_name: str = "Host"
    root_bus: int = 0
    root_ports: List[RootPortSwitchPortConfig] = field(default_factory=list)
    memory_ranges: List[MemoryRange] = field(default_factory=list)
    memory_controller: RootComplexMemoryControllerConfig


class RootComplex(RunnableComponent):
    def __init__(self, config: RootComplexConfig, label: Optional[str] = None):
        super().__init__(lambda class_name: f"{config.host_name}:{class_name}")

        root_complex_upstream_connection = CxlConnection()
        root_port_switch_upstream_connection = CxlConnection
        io_bridge_to_home_agent_memory_fifo = MemoryFifoPair()
        coh_bridge_to_home_agent_memory_fifo = MemoryFifoPair()
        home_agent_to_memory_controller_fifo = MemoryFifoPair()

        # Create CXL Root Port Switch
        if config.root_port_switch_type == ROOT_PORT_SWITCH_TYPE.PASS_THROUGH:
            root_port_switch_config = RootPortSwitchConfig(
                host_name=config.host_name,
                root_bus=config.root_bus,
                root_ports=config.root_ports,
                upstream_connection=root_port_switch_upstream_connection,
            )
            self._root_port_switch = SimpleRootPortSwitch(root_port_switch_config)
        else:
            raise Exception(
                f"Unsupported root port switch type {config.root_port_switch_type.name}"
            )

        # Create IO Bridge
        io_bridge_config = IoBridgeConfig(
            root_port_switch_upstream_connection.cfg_fifo,
            root_port_switch_upstream_connection.mmio_fifo,
            io_bridge_to_home_agent_memory_fifo,
            config.host_name,
        )
        self._io_bridge = IoBridge(io_bridge_config)

        # Create Cache Coherency Bridge
        cache_coherency_bridge_config = CacheCoherencyBridgeConfig(
            upstream_cxl_cache_fifos=root_complex_upstream_connection.cxl_cache_fifo,
            downstream_cxl_cache_fifos=root_port_switch_upstream_connection.cxl_cache_fifo,
            memory_producer_fifos=coh_bridge_to_home_agent_memory_fifo,
            host_name=config.host_name,
        )
        self._cache_coherency_bridge = CacheCoherencyBridge(cache_coherency_bridge_config)

        # Create Home Agent
        home_agent_config = HomeAgentConfig(
            upstream_cxl_mem_fifos=root_complex_upstream_connection.cxl_mem_fifo,
            downstream_cxl_mem_fifos=root_port_switch_upstream_connection.cxl_mem_fifo,
            memory_consumer_io_fifos=io_bridge_to_home_agent_memory_fifo,
            memory_consumer_coh_fifos=coh_bridge_to_home_agent_memory_fifo,
            memory_producer_fifos=home_agent_to_memory_controller_fifo,
            host_name=config.host_name,
            memory_ranges=config.memory_ranges,
        )
        self._home_agent = HomeAgent(home_agent_config)

        # Create Memory Controller
        memory_controller_config = MemoryControllerConfig(
            memory_size=config.memory_controller.memory_filename,
            memory_filename=config.memory_controller.memory_filename,
            host_name=config.host_name,
            memory_consumer_fifos=home_agent_to_memory_controller_fifo,
        )
        self._memory_controller = MemoryController(memory_controller_config)

    def get_root_bus(self) -> int:
        return self._root_port_switch.get_root_bus()

    def get_mmio_base_address(self) -> int:
        return 0x80000000

    async def write_config(self, bdf: int, offset: int, size: int, value: int):
        await self._io_bridge.write_config(bdf, offset, size, value)

    async def read_config(self, bdf: int, offset: int, size: int) -> int:
        return await self._io_bridge.read_config(bdf, offset, size)

    async def write_mmio(self, address: int, size: int, value: int):
        await self._io_bridge.write_mmio(address, size, value)

    async def read_mmio(self, address: int, size: int) -> int:
        return await self._io_bridge.read_mmio(address, size)

    async def _run(self):
        run_tasks = [
            asyncio.create_task(self._root_port_switch.run()),
            asyncio.create_task(self._io_bridge.run()),
            asyncio.create_task(self._cache_coherency_bridge.run()),
            asyncio.create_task(self._home_agent.run()),
            asyncio.create_task(self._memory_controller.run()),
        ]
        wait_tasks = [
            asyncio.create_task(self._root_port_switch.wait_for_ready()),
            asyncio.create_task(self._io_bridge.wait_for_ready()),
            asyncio.create_task(self._cache_coherency_bridge.wait_for_ready()),
            asyncio.create_task(self._home_agent.wait_for_ready()),
            asyncio.create_task(self._memory_controller.wait_for_ready()),
        ]
        await asyncio.gather(*wait_tasks)
        await self._change_status_to_running()
        await asyncio.gather(*run_tasks)

    async def _stop(self):
        tasks = [
            asyncio.create_task(self._root_port_switch.stop()),
            asyncio.create_task(self._io_bridge.stop()),
            asyncio.create_task(self._cache_coherency_bridge.stop()),
            asyncio.create_task(self._home_agent.stop()),
            asyncio.create_task(self._memory_controller.stop()),
        ]
        await asyncio.gather(*tasks)

"""
 Copyright (c) 2024, Eeum, Inc.

 This software is licensed under the terms of the Revised BSD License.
 See LICENSE for details.
"""

from typing import Optional, cast, List
from dataclasses import dataclass, field
from enum import Enum, auto
from abc import ABC, abstractmethod
import asyncio
from opencxl.util.logger import logger
from opencxl.util.component import RunnableComponent
from opencxl.util.pci import (
    extract_bus_from_bdf,
    extract_device_from_bdf,
    bdf_to_string,
)
from opencxl.cxl.transport.transaction import (
    CxlIoCfgRdPacket,
    CxlIoCfgWrPacket,
    CxlIoCompletionPacket,
    CxlIoCompletionWithDataPacket,
    CxlIoMemRdPacket,
    CxlIoMemWrPacket,
    is_cxl_io_completion_status_sc,
)
from opencxl.cxl.component.switch_connection_client import SwitchConnectionClient
from opencxl.cxl.component.cxl_component import CXL_COMPONENT_TYPE
from opencxl.cxl.component.cxl_connection import CxlConnection, FifoPair
from opencxl.pci.component.packet_processor import PacketProcessor


"""

TODO: Add an internal PCIe switch for routing PCIe packets between root ports

"""


class ROOT_PORT_SWITCH_TYPE(Enum):
    PASS_THROUGH = auto()
    PCIE_SWITCH = auto()


@dataclass
class RootPortConfig:
    device_num: int
    port_index: int


@dataclass
class RootComplexConfig:
    root_port_switch_type: ROOT_PORT_SWITCH_TYPE
    host_name: str = "Host"
    root_bus: int = 0
    root_ports: List[RootPortConfig] = field(default_factory=list)
    switch_host: str = "0.0.0.0"
    switch_port: int = 8000


class CxlRootPortDeviceClient(RunnableComponent):
    def __init__(
        self,
        port_index: int,
        root_bus: int,
        device_num: int,
        switch_host: str,
        switch_port: int,
        is_pass_through: bool = True,
        host_name: Optional[str] = None,
    ):
        host_prefix_label = f"{host_name}:" if host_name else ""
        super().__init__(lambda class_name: f"{host_prefix_label}{class_name}:Port{port_index}")
        if not is_pass_through:
            raise Exception("Only pass-through mode is supported")

        self._root_bus = root_bus
        self._is_pass_through = is_pass_through
        self._device_num = device_num
        self._switch_client = SwitchConnectionClient(
            port_index, CXL_COMPONENT_TYPE.R, host=switch_host, port=switch_port
        )

        self._upstream_connection = CxlConnection()
        downstream_connection = self._switch_client.get_cxl_connection()
        self._cxl_mem_processor = PacketProcessor(
            self._upstream_connection.cxl_mem_fifo,
            downstream_connection.cxl_mem_fifo,
            lambda class_name: f"{self.get_message_label()}:FifoRelay:CXL.mem",
        )
        self._cxl_cache_processor = PacketProcessor(
            self._upstream_connection.cxl_cache_fifo,
            downstream_connection.cxl_cache_fifo,
            lambda class_name: f"{self.get_message_label()}:FifoRelay:CXL.cache",
        )
        self._next_tag = 0

    def _get_secondary_bus(self) -> int:
        if self._is_pass_through:
            return self._root_bus + 1
        raise Exception("Only pass-through mode is supported")

    def get_cxl_connection(self) -> CxlConnection:
        return self._upstream_connection

    # TODO: Move this to RootPortSwitch class
    async def write_config(self, bdf: int, offset: int, size: int, value: int):
        bus = extract_bus_from_bdf(bdf)
        if self._root_bus == bus and self._is_pass_through:
            raise Exception("Accessing Root Port isn't supported under pass-through mode")

        bdf_string = bdf_to_string(bdf)
        is_type0 = bus == self._get_secondary_bus()
        if is_type0:
            # NOTE: For non-ARI component, only allow device 0
            device_num = extract_device_from_bdf(bdf)
            if device_num != 0:
                return

        packet = CxlIoCfgWrPacket.create(
            bdf, offset, size, value, is_type0, req_id=0, tag=self._next_tag
        )
        self._next_tag = (self._next_tag + 1) % 256

        cfg_fifo = self._switch_client.get_cxl_connection().cfg_fifo
        await cfg_fifo.host_to_target.put(packet)

        # TODO: Wait for an incoming packet that matchs tag
        packet = await cfg_fifo.target_to_host.get()

        tpl_type_str = "CFG WR0" if is_type0 else "CFG WR1"

        if not is_cxl_io_completion_status_sc(packet):
            cpl_packet = cast(CxlIoCompletionPacket, packet)
            logger.debug(
                self._create_message(
                    f"[{bdf_string}] {tpl_type_str} @ 0x{offset:x}[{size}B] : "
                    + f"Unsuccessful, Status: 0x{cpl_packet.cpl_header.status:x}"
                )
            )
            return

        logger.debug(
            self._create_message(
                f"[{bdf_string}] {tpl_type_str} @ 0x{offset:x}[{size}B] : 0x{value:x}"
            )
        )

    # TODO: Move this to RootPortSwitch class
    async def read_config(self, bdf: int, offset: int, size: int) -> int:
        if offset + size > ((offset // 4) + 1) * 4:
            raise Exception("offset + size out of DWORD boundary")

        bit_mask = (1 << size * 8) - 1

        bus = extract_bus_from_bdf(bdf)
        if self._root_bus == bus and self._is_pass_through:
            raise Exception("Accessing Root Port isn't supported under pass-through mode")

        bdf_string = bdf_to_string(bdf)
        is_type0 = bus == self._get_secondary_bus()
        if is_type0:
            # NOTE: For non-ARI component, only allow device 0
            device_num = extract_device_from_bdf(bdf)
            if device_num != 0:
                return 0xFFFFFFFF & bit_mask

        packet = CxlIoCfgRdPacket.create(bdf, offset, size, is_type0, req_id=0, tag=self._next_tag)
        self._next_tag = (self._next_tag + 1) % 256
        cfg_fifo = self._switch_client.get_cxl_connection().cfg_fifo
        await cfg_fifo.host_to_target.put(packet)

        # TODO: Wait for an incoming packet that matchs tag
        packet = await cfg_fifo.target_to_host.get()

        bit_offset = (offset % 4) * 8

        tpl_type_str = "CFG RD0" if is_type0 else "CFG RD1"

        if not is_cxl_io_completion_status_sc(packet):
            cpl_packet = cast(CxlIoCompletionPacket, packet)
            logger.debug(
                self._create_message(
                    f"[{bdf_string}] {tpl_type_str} @ 0x{offset:x}[{size}B] : "
                    + f"Unsuccessful, Status: 0x{cpl_packet.cpl_header.status:x}"
                )
            )
            return 0xFFFFFFFF & bit_mask

        cpld_packet = cast(CxlIoCompletionWithDataPacket, packet)
        data = (cpld_packet.data >> bit_offset) & bit_mask

        logger.debug(
            self._create_message(
                f"[{bdf_string}] {tpl_type_str} @ 0x{offset:x}[{size}B] : 0x{data:x}"
            )
        )
        return data

    # TODO: Move this to RootPortSwitch class
    async def write_mmio(self, address: int, size: int, value: int):
        message = self._create_message(f"MMIO: Writing 0x{value:08x} to 0x{address:08x}")
        logger.debug(message)
        packet = CxlIoMemWrPacket.create(address, size, value)
        print(packet.get_pretty_string())
        print(packet.get_hex_dump())
        await self._switch_client.get_cxl_connection().mmio_fifo.host_to_target.put(packet)

    # TODO: Move this to RootPortSwitch class
    async def read_mmio(self, address: int, size: int) -> CxlIoCompletionWithDataPacket:
        message = self._create_message(f"MMIO: Reading data from 0x{address:08x}")
        logger.debug(message)
        packet = CxlIoMemRdPacket.create(address, size)
        await self._switch_client.get_cxl_connection().mmio_fifo.host_to_target.put(packet)

        packet = await self._switch_client.get_cxl_connection().mmio_fifo.target_to_host.get()
        assert is_cxl_io_completion_status_sc(packet)
        cpld_packet = cast(CxlIoCompletionWithDataPacket, packet)
        return cpld_packet.data

    async def _run(self):
        run_tasks = [
            asyncio.create_task(self._switch_client.run()),
            asyncio.create_task(self._cxl_mem_processor.run()),
            asyncio.create_task(self._cxl_cache_processor.run()),
        ]
        wait_tasks = [
            asyncio.create_task(self._switch_client.wait_for_ready()),
            asyncio.create_task(self._cxl_mem_processor.wait_for_ready()),
            asyncio.create_task(self._cxl_cache_processor.wait_for_ready()),
        ]
        await asyncio.gather(*wait_tasks)
        await self._change_status_to_running()
        await asyncio.gather(*run_tasks)

    async def _stop(self):
        tasks = [
            asyncio.create_task(self._switch_client.stop()),
            asyncio.create_task(self._cxl_mem_processor.stop()),
            asyncio.create_task(self._cxl_cache_processor.stop()),
        ]
        await asyncio.gather(*tasks)


class RootPortSwitchBase(RunnableComponent, ABC):
    @abstractmethod
    def get_root_bus(self) -> int:
        raise Exception("get_root_bus must be implemented by the child class")

    @abstractmethod
    async def write_config(self, bdf: int, offset: int, size: int, value: int):
        raise Exception("write_config must be implemented by the child class")

    @abstractmethod
    async def read_config(self, bdf: int, offset: int, size: int) -> int:
        raise Exception("read_config must be implemented by the child class")

    @abstractmethod
    async def write_mmio(self, address: int, size: int, value: int):
        raise Exception("write_mmio must be implemented by the child class")

    @abstractmethod
    async def read_mmio(self, address: int, size: int) -> int:
        raise Exception("read_mmio must be implemented by the child class")


class SimpleRootPortSwitch(RootPortSwitchBase):
    def __init__(self, config: RootComplexConfig):
        super().__init__(lambda class_name: f"{config.host_name}:{class_name}")
        port_index = 0
        device_num = 1
        is_pass_through = True
        self._root_port_device_client = CxlRootPortDeviceClient(
            port_index,
            config.root_bus,
            device_num,
            config.switch_host,
            config.switch_port,
            is_pass_through,
            config.host_name,
        )
        self._root_bus_num = config.root_bus + 1
        self._cxl_mem_fifo_pair = FifoPair()
        self._cxl_cache_fifo_pair = FifoPair()
        downstream_fifo = self._root_port_device_client.get_cxl_connection()
        self._cxl_mem_relay = PacketProcessor(self._cxl_mem_fifo_pair, downstream_fifo.cxl_mem_fifo)
        self._cxl_cache_relay = PacketProcessor(
            self._cxl_cache_fifo_pair,
            downstream_fifo.cxl_cache_fifo,
        )

    def get_cxl_mem_fifo_pair(self) -> FifoPair:
        return self._cxl_mem_fifo_pair

    def get_cxl_cache_fifo_pair(self) -> FifoPair:
        return self._cxl_cache_fifo_pair

    async def write_config(self, bdf: int, offset: int, size: int, value: int):
        await self._root_port_device_client.write_config(bdf, offset, size, value)

    async def read_config(self, bdf: int, offset: int, size: int) -> int:
        return await self._root_port_device_client.read_config(bdf, offset, size)

    async def write_mmio(self, address: int, size: int, value: int):
        await self._root_port_device_client.write_mmio(address, size, value)

    async def read_mmio(self, address: int, size: int) -> int:
        return await self._root_port_device_client.read_mmio(address, size)

    def get_root_bus(self) -> int:
        return self._root_bus_num

    async def _run(self):
        start_tasks = [
            asyncio.create_task(self._root_port_device_client.run()),
            asyncio.create_task(self._cxl_mem_relay.run()),
            asyncio.create_task(self._cxl_cache_relay.run()),
        ]
        wait_tasks = [
            asyncio.create_task(self._root_port_device_client.wait_for_ready()),
            asyncio.create_task(self._cxl_mem_relay.wait_for_ready()),
            asyncio.create_task(self._cxl_cache_relay.wait_for_ready()),
        ]
        await asyncio.gather(*wait_tasks)
        await self._change_status_to_running()
        await asyncio.gather(*start_tasks)

    async def _stop(self):
        tasks = [
            asyncio.create_task(self._root_port_device_client.stop()),
            asyncio.create_task(self._cxl_mem_relay.stop()),
            asyncio.create_task(self._cxl_cache_relay.stop()),
        ]
        await asyncio.gather(*tasks)


class RootComplex(RunnableComponent):
    def __init__(self, config: RootComplexConfig, label: Optional[str] = None):
        super().__init__(lambda class_name: f"{config.host_name}:{class_name}")

        if config.root_port_switch_type == ROOT_PORT_SWITCH_TYPE.PASS_THROUGH:
            # NOTE: Use get_cxl_mem_fifo_pair() and get_cxl_cache_fifo_pair() to send and receive CXL.mem and CXL.cache packets
            # from other HW modules such as Home Agent or Coh Bridge.
            self._root_port_switch = SimpleRootPortSwitch(config)
        else:
            raise Exception(
                f"Unsupported root port switch type {config.root_port_switch_type.name}"
            )

    def get_root_bus(self) -> int:
        return self._root_port_switch.get_root_bus()

    def get_mmio_base_address(self) -> int:
        return 0x80000000

    async def write_config(self, bdf: int, offset: int, size: int, value: int):
        await self._root_port_switch.write_config(bdf, offset, size, value)

    async def read_config(self, bdf: int, offset: int, size: int) -> int:
        return await self._root_port_switch.read_config(bdf, offset, size)

    async def write_mmio(self, address: int, size: int, value: int):
        await self._root_port_switch.write_mmio(address, size, value)

    async def read_mmio(self, address: int, size: int) -> int:
        return await self._root_port_switch.read_mmio(address, size)

    async def _run(self):
        tasks = [
            asyncio.create_task(self._root_port_switch.run()),
        ]
        await self._root_port_switch.wait_for_ready()
        await self._change_status_to_running()
        await asyncio.gather(*tasks)

    async def _stop(self):
        tasks = [asyncio.create_task(self._root_port_switch.stop())]
        await asyncio.gather(*tasks)

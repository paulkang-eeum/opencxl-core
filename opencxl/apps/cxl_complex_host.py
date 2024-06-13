"""
 Copyright (c) 2024, Eeum, Inc.

 This software is licensed under the terms of the Revised BSD License.
 See LICENSE for details.
"""

import asyncio
from typing import Optional
from opencxl.util.component import RunnableComponent
from opencxl.cxl.component.root_complex import RootComplex, RootComplexConfig


class CxlComplexHost(RunnableComponent):
    def __init__(
        self,
        root_complex_config: RootComplexConfig,
        label: Optional[str] = None,
    ):
        super().__init__(label)
        self._root_complex = RootComplex(root_complex_config)

    def get_root_complex(self):
        return self._root_complex

    async def write_config(self, bdf: int, offset: int, size: int, value: int):
        await self._root_complex.write_config(bdf, offset, size, value)

    async def read_config(self, bdf: int, offset: int, size: int) -> int:
        return await self._root_complex.read_config(bdf, offset, size)

    async def write_mmio(self, address: int, size: int, value: int):
        await self._root_complex.write_mmio(address, size, value)

    async def read_mmio(self, address: int, size: int) -> int:
        return await self._root_complex.read_mmio(address, size)

    async def _run(self):
        tasks = [asyncio.create_task(self._root_complex.run())]
        await self._root_complex.wait_for_ready()
        await self._change_status_to_running()
        await asyncio.gather(*tasks)

    async def _stop(self):
        tasks = [asyncio.create_task(self._root_complex.stop())]
        await asyncio.gather(*tasks)

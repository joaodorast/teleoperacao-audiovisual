"""Buffers entre as threads de captura e o laço assíncrono de transmissão.

``LatestSlot`` guarda **somente o item mais recente** (vídeo): se o transmissor ainda não
consumiu o quadro anterior quando um novo chega, o anterior é descartado. Assim nunca se
acumulam quadros antigos, conforme a estratégia de latência do projeto.

``BoundedAudioQueue`` guarda poucos blocos de áudio: o suficiente para absorver variações
de agendamento sem cortes (continuidade), mas limitado para que o atraso não cresça.
"""

from __future__ import annotations

import asyncio
import threading
from collections import deque
from typing import Generic, TypeVar

T = TypeVar("T")


class LatestSlot(Generic[T]):
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._item: T | None = None
        self._seq = 0
        self._consumed_seq = 0
        self._dropped = 0
        self._waiters: set[tuple[asyncio.AbstractEventLoop, asyncio.Event]] = set()

    @property
    def dropped(self) -> int:
        """Quadros substituídos antes de serem consumidos."""
        return self._dropped

    @property
    def seq(self) -> int:
        return self._seq

    def put(self, item: T) -> None:
        """Pode ser chamado de qualquer thread."""
        with self._lock:
            if self._seq > self._consumed_seq and self._item is not None:
                self._dropped += 1
            self._item = item
            self._seq += 1
            waiters = list(self._waiters)
        for loop, event in waiters:
            try:
                loop.call_soon_threadsafe(event.set)
            except RuntimeError:  # laço encerrado
                with self._lock:
                    self._waiters.discard((loop, event))

    def peek(self) -> tuple[int, T | None]:
        with self._lock:
            return self._seq, self._item

    def clear(self) -> None:
        with self._lock:
            self._item = None
            self._consumed_seq = self._seq

    async def get_newer(self, last_seq: int, timeout: float | None = None) -> tuple[int, T]:
        """Aguarda um item com sequência maior que ``last_seq`` e devolve o mais recente."""
        loop = asyncio.get_running_loop()
        event = asyncio.Event()
        key = (loop, event)
        with self._lock:
            self._waiters.add(key)
        try:
            while True:
                with self._lock:
                    if self._seq > last_seq and self._item is not None:
                        self._consumed_seq = self._seq
                        return self._seq, self._item
                    event.clear()
                if timeout is None:
                    await event.wait()
                else:
                    await asyncio.wait_for(event.wait(), timeout)
        finally:
            with self._lock:
                self._waiters.discard(key)


class BoundedAudioQueue:
    """Fila de blocos PCM com capacidade máxima; descarta os mais antigos se exceder."""

    def __init__(self, max_blocks: int) -> None:
        if max_blocks < 1:
            raise ValueError("max_blocks deve ser >= 1")
        self._lock = threading.Lock()
        self._blocks: deque[bytes] = deque()
        self._max = max_blocks
        self._dropped = 0
        self._waiters: set[tuple[asyncio.AbstractEventLoop, asyncio.Event]] = set()

    @property
    def dropped(self) -> int:
        return self._dropped

    def __len__(self) -> int:
        with self._lock:
            return len(self._blocks)

    def put(self, block: bytes) -> None:
        with self._lock:
            self._blocks.append(block)
            while len(self._blocks) > self._max:
                self._blocks.popleft()
                self._dropped += 1
            waiters = list(self._waiters)
        for loop, event in waiters:
            try:
                loop.call_soon_threadsafe(event.set)
            except RuntimeError:
                with self._lock:
                    self._waiters.discard((loop, event))

    def get_nowait(self) -> bytes | None:
        with self._lock:
            return self._blocks.popleft() if self._blocks else None

    def clear(self) -> None:
        with self._lock:
            self._blocks.clear()

    async def get(self, timeout: float) -> bytes | None:
        """Devolve o próximo bloco ou ``None`` se nada chegar dentro de ``timeout``."""
        loop = asyncio.get_running_loop()
        event = asyncio.Event()
        key = (loop, event)
        with self._lock:
            if self._blocks:
                return self._blocks.popleft()
            self._waiters.add(key)
        try:
            deadline = loop.time() + timeout
            while True:
                with self._lock:
                    if self._blocks:
                        return self._blocks.popleft()
                    event.clear()
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return None
                try:
                    await asyncio.wait_for(event.wait(), remaining)
                except asyncio.TimeoutError:
                    return None
        finally:
            with self._lock:
                self._waiters.discard(key)

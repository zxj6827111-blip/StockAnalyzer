"""Feishu long-connection runner built on the official Python SDK."""

from __future__ import annotations

import asyncio
import importlib
import queue
import threading
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime
from typing import Any

from stock_analyzer.command.feishu_interaction import (
    FeishuMessageEvent,
    build_feishu_message_event_from_sdk,
)


class FeishuLongConnectionRunner:
    def __init__(
        self,
        *,
        app_id: str,
        app_secret: str,
        message_handler: Callable[[FeishuMessageEvent], None],
        debug: bool = False,
    ) -> None:
        self._app_id = app_id.strip()
        self._app_secret = app_secret.strip()
        self._message_handler = message_handler
        self._debug = debug
        self._thread: threading.Thread | None = None
        self._worker_thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client: Any = None
        self._lock = threading.Lock()
        self._stop_requested = threading.Event()
        self._status = "idle"
        self._last_error = ""
        self._started_at = ""
        self._last_message_at = ""
        self._messages_handled = 0
        self._message_queue: queue.Queue[FeishuMessageEvent | None] = queue.Queue()

    def start(self) -> bool:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop_requested.clear()
            self._status = "starting"
            self._last_error = ""
            self._messages_handled = 0
            self._message_queue = queue.Queue()
            self._worker_thread = threading.Thread(
                target=self._worker_loop,
                name="feishu-long-connection-worker",
                daemon=True,
            )
            self._worker_thread.start()
            self._thread = threading.Thread(
                target=self._run,
                name="feishu-long-connection",
                daemon=True,
            )
            self._thread.start()
            return True

    def stop(self, timeout_sec: float = 5.0) -> None:
        self._stop_requested.set()
        loop = self._loop
        client = self._client
        if loop is not None and client is not None and hasattr(client, "_disconnect"):
            with suppress(Exception):
                future = asyncio.run_coroutine_threadsafe(client._disconnect(), loop)
                future.result(timeout=timeout_sec)
        if loop is not None:
            with suppress(Exception):
                loop.call_soon_threadsafe(loop.stop)
        with suppress(Exception):
            self._message_queue.put_nowait(None)
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout_sec)
        worker_thread = self._worker_thread
        if worker_thread is not None and worker_thread.is_alive():
            worker_thread.join(timeout=timeout_sec)
        with self._lock:
            self._status = "stopped"

    def status(self) -> dict[str, object]:
        thread = self._thread
        worker_thread = self._worker_thread
        return {
            "status": self._status,
            "thread_alive": bool(thread is not None and thread.is_alive()),
            "worker_alive": bool(worker_thread is not None and worker_thread.is_alive()),
            "started_at": self._started_at,
            "last_message_at": self._last_message_at,
            "last_error": self._last_error,
            "messages_handled": self._messages_handled,
            "queue_size": self._message_queue.qsize(),
        }

    def _run(self) -> None:
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            lark = importlib.import_module("lark_oapi")
            event_handler = (
                lark.EventDispatcherHandler.builder("", "")
                .register_p2_im_message_receive_v1(self._on_sdk_message)
                .build()
            )
            self._client = lark.ws.Client(
                self._app_id,
                self._app_secret,
                event_handler=event_handler,
                log_level=lark.LogLevel.DEBUG if self._debug else lark.LogLevel.INFO,
            )
            with self._lock:
                self._status = "running"
                self._started_at = datetime.now().isoformat()
            self._client.start()
            with self._lock:
                self._status = "stopped" if self._stop_requested.is_set() else "exited"
        except Exception as exc:
            with self._lock:
                self._last_error = str(exc)
                self._status = "stopped" if self._stop_requested.is_set() else "error"
        finally:
            if self._loop is not None:
                with suppress(Exception):
                    self._loop.close()
            self._loop = None
            self._client = None
            self._thread = None

    def _on_sdk_message(self, data: Any) -> None:
        event = build_feishu_message_event_from_sdk(data)
        with self._lock:
            self._last_message_at = datetime.now().isoformat()
        self._message_queue.put_nowait(event)

    def _worker_loop(self) -> None:
        while not self._stop_requested.is_set():
            try:
                event = self._message_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if event is None:
                break
            try:
                self._message_handler(event)
                with self._lock:
                    self._messages_handled += 1
            except Exception as exc:
                with self._lock:
                    self._last_error = str(exc)
            finally:
                self._message_queue.task_done()
        self._worker_thread = None

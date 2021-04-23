# Copyright (c) 2021 Yubico AB
# All rights reserved.
#
#   Redistribution and use in source and binary forms, with or
#   without modification, are permitted provided that the following
#   conditions are met:
#
#    1. Redistributions of source code must retain the above copyright
#       notice, this list of conditions and the following disclaimer.
#    2. Redistributions in binary form must reproduce the above
#       copyright notice, this list of conditions and the following
#       disclaimer in the documentation and/or other materials provided
#       with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS
# FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE
# COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT,
# INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING,
# BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
# LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
# LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN
# ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.


from .base import RpcNode, child, action, NoSuchNodeException
from .oath import OathNode
from .fido import FidoNode
from .management import ManagementNode
from .. import __version__ as ykman_version
from ..device import (
    scan_devices,
    list_all_devices,
    get_name,
    read_info,
    connect_to_device,
)
from ..diagnostics import get_diagnostics
from yubikit.core import TRANSPORT
from yubikit.core.smartcard import SmartCardConnection
from yubikit.core.otp import OtpConnection
from yubikit.core.fido import FidoConnection
from yubikit.management import USB_INTERFACE

from ..pcsc import list_devices, YK_READER_NAME
from smartcard.Exceptions import SmartcardException
from queue import Queue
from threading import Thread, Event
from dataclasses import asdict
from typing import Callable, Dict, List

import os
import logging

logger = logging.getLogger(__name__)


_SESSION_NODES = dict(management=ManagementNode, oath=OathNode, fido=FidoNode)


class RootNode(RpcNode):
    def __init__(self):
        super().__init__()
        self._devices = DevicesNode()
        self._readers = ReadersNode()

    def __call__(self, *args):
        result = super().__call__(*args)
        if result is None:
            result = {}
        return result

    def get_data(self):
        return dict(version=ykman_version)

    @child
    def usb(self):
        return self._devices

    @child
    def nfc(self):
        return self._readers

    @action
    def diagnose(self, *ignored):
        return dict(diagnostics=get_diagnostics())


class ReadersNode(RpcNode):
    def __init__(self):
        super().__init__()
        self._state = set()
        self._readers = {}
        self._reader_mapping = {}

    def list_children(self):
        devices = [
            d for d in list_devices("") if YK_READER_NAME not in d.reader.name.lower()
        ]
        state = {d.reader.name for d in devices}
        if self._state != state:
            self._readers = {}
            self._reader_mapping = {}
            for device in devices:
                dev_id = os.urandom(4).hex()
                self._reader_mapping[dev_id] = device
                self._readers[dev_id] = dict(name=device.reader.name)
            self._state = state
        return self._readers

    def create_child(self, name):
        return DeviceNode(self._reader_mapping[name], None)


class DevicesNode(RpcNode):
    def __init__(self):
        super().__init__()
        self._state = 0
        self._devices = {}
        self._device_mapping = {}

    def list_children(self):
        state = scan_devices()[1]
        if state != self._state:
            self._devices = {}
            self._device_mapping = {}
            for dev, info in list_all_devices():
                dev_id = str(info.serial) if info.serial else os.urandom(4).hex()
                while dev_id in self._device_mapping:
                    dev_id = os.urandom(4).hex()
                self._device_mapping[dev_id] = (dev, info)
                name = get_name(info, dev.pid.get_type() if dev.pid else None)
                self._devices[dev_id] = dict(pid=dev.pid, name=name, serial=info.serial)
            self._state = state
        return self._devices

    def create_child(self, name):
        return DeviceNode(*self._device_mapping[name])


class DeviceNode(RpcNode):
    def __init__(self, device, info=None):
        super().__init__()
        self._device = device
        self._info = info

    def __call__(self, *args):
        try:
            return super().__call__(*args)
        except (SmartcardException, OSError) as e:
            logger.error("Device error", exc_info=e)
            self._child = None
            name = self._child_name
            self._child_name = None
            raise NoSuchNodeException(name)

    def _create_connection(self, conn_type):
        if self._device.supports_connection(conn_type):
            connection = self._device.open_connection(conn_type)
        elif self._info and self._info.serial:
            connection = connect_to_device(self._info.serial, [conn_type])[0]
        else:
            # TODO: Make sure there's only one device
            connection = connect_to_device(self._info.serial, [conn_type])[0]
            # raise ValueError("Unsupported connection type")
        return ConnectionNode(self._device.transport, connection)

    def list_children(self):
        children = super().list_children()
        if self._device.transport == TRANSPORT.USB:
            enabled = self._device.pid.get_interfaces()
        else:  # NFC, only ccid and FIDO
            enabled = USB_INTERFACE.CCID | USB_INTERFACE.FIDO
        for iface in USB_INTERFACE:
            if iface not in enabled:
                del children[iface.name.lower()]
        return children

    @child
    def ccid(self):
        return self._create_connection(SmartCardConnection)

    @child
    def otp(self):
        return self._create_connection(OtpConnection)

    @child
    def fido(self):
        return self._create_connection(FidoConnection)

    def get_data(self):
        for conn_type in (SmartCardConnection, OtpConnection, FidoConnection):
            if self._device.supports_connection(conn_type):
                with self._device.open_connection(conn_type) as conn:
                    pid = self._device.pid
                    info = read_info(pid, conn)
                    name = get_name(info, pid.get_type() if pid else None)
                    return dict(
                        pid=pid,
                        name=name,
                        transport=self._device.transport,
                        info=asdict(info),
                    )
        raise ValueError("No supported connections")


class ConnectionNode(RpcNode):
    def __init__(self, transport, connection):
        super().__init__()
        self._transport = transport
        self._connection = connection
        self._info = read_info(None, self._connection)
        self._capabilities = self._info.config.enabled_capabilities[transport]

    def close(self):
        super().close()
        self._connection.close()

    def list_children(self):
        return {session: {} for session in _SESSION_NODES}

    def create_child(self, name):
        if name in _SESSION_NODES:
            return _SESSION_NODES[name](self._connection)
        return super().create_child(name)

    def get_data(self):
        info = read_info(None, self._connection)
        return {"version": info.version, "serial": info.serial}


def _handle_incoming(event, recv, error, cmd_queue):
    while True:
        request = recv()
        if not request:
            break
        try:
            if "signal" in request:
                # Cancel signals are handled here, the rest forwarded
                if request["signal"] == "cancel":
                    event.set()
                else:
                    # Ignore other signals
                    logger.error("Unhandled signal: %r", request)
            elif "action" in request:
                cmd_queue.join()  # Wait for existing command to complete
                event.clear()  # Reset event for next command
                cmd_queue.put(request)
            else:
                error(Exception("Unsupported message type"))
        except Exception as e:
            error(e)
    event.set()
    cmd_queue.put(None)


def process(
    send: Callable[[Dict], None],
    recv: Callable[[], Dict],
    handler: Callable[[str, List, Dict, Event, Callable[[str], None]], Dict],
) -> None:
    def error(e):
        logger.error("Returning error", exc_info=e)
        send(dict(result="error", message=str(e)))

    def signal(name: str, **kwargs):
        send(dict(signal=name, **kwargs))

    def success(data: Dict):
        send(dict(result="success", **data))

    event = Event()
    cmd_queue: Queue = Queue(1)
    read_thread = Thread(target=_handle_incoming, args=(event, recv, error, cmd_queue))
    read_thread.start()

    while True:
        request = cmd_queue.get()
        if request is None:
            break
        try:
            success(
                handler(
                    request.pop("action"),
                    request.pop("target", []),
                    request.pop("params", {}),
                    event,
                    signal,
                )
            )
        except Exception as e:
            error(e)
        cmd_queue.task_done()

    read_thread.join()


def run_rpc(
    send: Callable[[Dict], None],
    recv: Callable[[], Dict],
):
    process(send, recv, RootNode())

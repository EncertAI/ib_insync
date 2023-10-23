from __future__ import annotations

import asyncio
import base64
import json
import struct
from collections import defaultdict
from pathlib import Path

import pytest

import ib_insync as ibi


def get_fields(buffer: bytes) -> tuple[list[str], bytes, bytes]:
    fields = []
    data = b""
    msgEnd = 0

    # 4 byte prefix tells the message length
    if len(buffer) >= 4:
        msgEnd = 4 + struct.unpack('>I', buffer[:4])[0]

    if msgEnd and len(buffer) >= msgEnd:
        data = buffer[:msgEnd]
        msg = data[4:].decode(errors='backslashreplace')
        fields = msg.split('\0')[:-1]
        buffer = buffer[msgEnd:]

    return fields, buffer, data


class FakeTwsStreamReader:
    queue: asyncio.Queue[tuple[str, str]]

    def __init__(self, json_file: Path):
        with open(json_file) as f:
            self.request_response = json.load(f)
        self.queue = asyncio.Queue()
        self.response = None

    async def read(self, n: int = -1) -> bytes:
        if self.response is None:
            encoded_request, reqId = await self.queue.get()
            if encoded_request == "CLOSE":
                return b""
            self.response = self.request_response[encoded_request]
            self.reqId = reqId
            self.index = 0
        encoded_data = self.response[self.index]
        data = base64.b64decode(encoded_data)
        if self.reqId:
            fields = data[4:].decode(errors='backslashreplace').split('\0')
            if fields[0] != '8':
                fields[2] = self.reqId
                data = data[:4] + ('\0'.join(fields)).encode()
        self.index += 1
        if self.index == len(self.response):
            self.response = None
        return data


class FakeTwsStreamWriter:
    def __init__(self, queue: asyncio.Queue[tuple[str, str]]):
        self.queue = queue

    def write(self, request: bytes):
        encoded_request, reqId = request.decode(errors='backslashreplace').split('\0')
        self.queue.put_nowait((encoded_request, reqId))

    def close(self):
        self.queue.put_nowait(("CLOSE", ""))


def open_fake_tws_connection(json_file: Path) -> tuple[FakeTwsStreamReader, FakeTwsStreamWriter]:
    reader = FakeTwsStreamReader(json_file)
    writer = FakeTwsStreamWriter(reader.queue)
    return reader, writer


class TwsStub:
    StreamReader = asyncio.StreamReader | FakeTwsStreamReader
    StreamWriter = asyncio.StreamWriter | FakeTwsStreamWriter
    request_response: dict[str, list[str]]

    def __init__(
        self,
        stub: bool = True,
        stub_port: int = 7497,
        tws_host: str = '192.168.1.234',
        tws_port: int = 7497,
    ):
        self.stub = stub
        self.stub_port = stub_port
        self.tws_host = tws_host
        self.tws_port = tws_port
        file_parent = Path(__file__).parent.resolve()
        self.json_file = file_parent / 'request_response.json'

    async def start(self) -> 'TwsStub':
        self.stub_server = await asyncio.start_server(self.handle_connection, 'localhost', self.stub_port)
        return self

    async def handle_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        if not self.stub:
            tws_reader, tws_writer = await asyncio.open_connection(self.tws_host, self.tws_port)
            self.request_response = defaultdict(list)
            self.current_request = None
        else:
            tws_reader, tws_writer = open_fake_tws_connection(self.json_file)

        await asyncio.gather(
            self.response_reader(tws_reader, writer),
            self.request_writer(reader, tws_writer),
        )

    async def response_reader(self, tws_reader: StreamReader, writer: asyncio.StreamWriter):
        buffer = b""
        while True:
            data = await tws_reader.read(1 << 12)
            if not data:
                break

            buffer += data
            while buffer:
                fields, buffer, data = get_fields(buffer)
                if not fields:
                    break
                print(f"TwsStub response: {fields}")
                if not self.stub:
                    assert self.current_request
                    encoded_data = base64.b64encode(data).decode()
                    self.request_response[self.current_request].append(encoded_data)
                writer.write(data)

    async def request_writer(self, reader: asyncio.StreamReader, tws_writer: StreamWriter):
        first_request = True
        buffer = b""
        while True:
            data = await reader.read(1 << 12)
            if not data:
                break

            buffer += data
            while buffer:
                if first_request:
                    msg = buffer.decode(errors='backslashreplace')
                    fields = msg.split('\0')
                    first_request = False
                    buffer = b""
                else:
                    fields, buffer, data = get_fields(buffer)
                if not fields:
                    break
                encoded_request = fields[0] + '_' + fields[1]
                self.current_request = encoded_request
                print(f"\nTwsStub request: {fields} {data}")
                if not self.stub:
                    tws_writer.write(data)
                    await asyncio.sleep(0.1)
                else:
                    code = int(fields[0]) if fields[0].isdigit() else 0
                    reqId = fields[2] if code in [7, 62, 76] else ""
                    data = (encoded_request + '\0' + reqId).encode()
                    tws_writer.write(data)

        tws_writer.close()

    def close(self):
        self.stub_server.close()

    async def wait_closed(self):
        await self.stub_server.wait_closed()
        if not self.stub:
            with open(self.json_file, 'w') as f:
                json.dump(self.request_response, f)


@pytest.fixture(scope='session')
def event_loop():
    loop = ibi.util.getLoop()
    yield loop
    loop.close()


@pytest.fixture(scope='session')
async def ib():
    tws = await TwsStub().start()
    ib = ibi.IB()
    await ib.connectAsync()
    yield ib
    await ib.disconnectAsync()
    tws.close()
    await tws.wait_closed()


if __name__ == '__main__':
    async def main():
        tws = await TwsStub().start()
        ib = ibi.IB()
        await ib.connectAsync()
        summary = await ib.accountSummaryAsync()
        assert summary
        await ib.disconnectAsync()
        tws.close()
        await tws.wait_closed()

    asyncio.run(main())

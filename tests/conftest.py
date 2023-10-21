import asyncio
import base64
from collections import defaultdict
import json
import pathlib

import pytest

import ib_insync as ibi


class JsonReader:
    def __init__(self, json_file: str):
        with open(json_file) as f:
            self.request_response = json.load(f)
        self.event = asyncio.Event()

    def write(self, request: bytes):
        encoded_request = base64.b64encode(request).decode('utf-8')
        if encoded_request not in self.request_response:
            raise ValueError(f"Unknown request: {request} {encoded_request}")
        self.response = self.request_response[encoded_request]
        self.index = 0
        self.event.set()

    async def read(self, n: int = -1) -> bytes:
        await self.event.wait()
        encoded_data = self.response[self.index]
        data = base64.b64decode(encoded_data)
        self.index += 1
        if self.index == len(self.response):
            self.event.clear()
        return data

    def close(self):
        self.response = [""]
        self.index = 0
        self.event.set()


class TwsStub:
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
        file_parent = pathlib.Path(__file__).parent.resolve()
        self.json_file = file_parent / 'request_response.json'

    async def start(self) -> 'TwsStub':
        self.stub_server = await asyncio.start_server(self.handle_connection, 'localhost', self.stub_port)
        return self

    async def handle_connection(self, reader, writer):
        if not self.stub:
            tws_reader, tws_writer = await asyncio.open_connection(self.tws_host, self.tws_port)
            self.request_response = defaultdict(list)
            self.current_request = None
        else:
            reader_writer = JsonReader(self.json_file)
            tws_reader, tws_writer = reader_writer, reader_writer

        await asyncio.gather(
            self.response_reader(tws_reader, writer),
            self.request_writer(reader, tws_writer),
        )

    async def response_reader(self, tws_reader, writer):
        while True:
            data = await tws_reader.read(1 << 12)
            if not data:
                break
            if not self.stub:
                encoded_data = base64.b64encode(data).decode('utf-8')
                self.request_response[self.current_request].append(encoded_data)
                print(".", end="")
            writer.write(data)
        tws_reader.close()

    async def request_writer(self, reader, tws_writer):
        while True:
            data = await reader.read(1 << 12)
            if not data:
                break
            encoded_data = base64.b64encode(data).decode('utf-8')
            print(f"\nTwsStub request: {data[:32]} {encoded_data[:128]}")
            self.current_request = encoded_data
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
    ib.disconnect()
    tws.close()
    await tws.wait_closed()

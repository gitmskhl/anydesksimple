import asyncio
import struct
import unittest

from protocol import MAX_PACKET, encode_packet, read_packet


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_fragmented_packet(self):
        reader = asyncio.StreamReader()
        packet = encode_packet({"type": "request", "name": "Иван"})

        async def feed():
            for byte in packet:
                reader.feed_data(bytes([byte]))
                await asyncio.sleep(0)
        task = asyncio.create_task(feed())
        self.assertEqual(await read_packet(reader), {"type": "request", "name": "Иван"})
        await task

    async def test_oversized_header_is_rejected_before_body(self):
        reader = asyncio.StreamReader()
        reader.feed_data(struct.pack("!I", MAX_PACKET + 1))
        with self.assertRaises(ValueError):
            await read_packet(reader)

    async def test_non_object_is_rejected(self):
        reader = asyncio.StreamReader()
        reader.feed_data(struct.pack("!I", 2) + b"[]")
        with self.assertRaises(ValueError):
            await read_packet(reader)

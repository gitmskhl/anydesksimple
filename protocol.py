"""Ограниченные JSON-пакеты; никаких pickle / выполнения полученного кода."""
import asyncio
import json
import struct

MAX_PACKET = 256 * 1024


def encode_packet(message):
    payload = json.dumps(message, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(payload) > MAX_PACKET:
        raise ValueError("Слишком большой пакет")
    return struct.pack("!I", len(payload)) + payload


async def read_packet(reader):
    size = struct.unpack("!I", await reader.readexactly(4))[0]
    if not 0 < size <= MAX_PACKET:
        raise ValueError("Недопустимый размер пакета")
    message = json.loads(await reader.readexactly(size))
    if not isinstance(message, dict) or not isinstance(message.get("type"), str):
        raise ValueError("Ожидался JSON-объект с полем type")
    return message


async def write_packet(writer, message):
    # write() не содержит await: параллельные корутины не перемешивают пакеты.
    writer.write(encode_packet(message))
    await asyncio.wait_for(writer.drain(), 10)

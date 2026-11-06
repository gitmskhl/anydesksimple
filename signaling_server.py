"""TLS signaling: коды подключения, согласие, SDP. Медиа не пересылает."""
import argparse
import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import secrets
import socket
import ssl

import config
from protocol import read_packet, write_packet


def init_certificate():
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    if config.SIGNALING_CERT.exists() or config.SIGNALING_KEY.exists():
        raise SystemExit("Сертификат или ключ уже существует; перезапись отменена.")
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "MiniDesk signaling")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=365))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .sign(key, hashes.SHA256()))
    config.SIGNALING_KEY.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    config.SIGNALING_CERT.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    print("Созданы signaling_cert.pem и signaling_key.pem.")
    print("На оба ПК скопируйте ТОЛЬКО signaling_cert.pem по доверенному каналу.")


@dataclass
class Room:
    host: asyncio.StreamWriter
    secret: str
    guest: asyncio.StreamWriter | None = None
    phase: str = "waiting"


class SignalingServer:
    def __init__(self):
        self.rooms = {}
        self.connections = set()

    async def handle(self, reader, writer):
        room = None
        room_id = None
        is_host = False
        if len(self.connections) >= 128:
            writer.close()
            return
        self.connections.add(writer)
        try:
            first = await asyncio.wait_for(read_packet(reader), 10)
            if first["type"] == "register":
                is_host = True
                room_id = secrets.token_hex(4).upper()
                while room_id in self.rooms:
                    room_id = secrets.token_hex(4).upper()
                room = Room(writer, secrets.token_urlsafe(18))
                self.rooms[room_id] = room
                await write_packet(writer, {"type": "registered",
                                           "code": f"{room_id}:{room.secret}"})
            elif first["type"] == "join":
                code = str(first.get("code", ""))
                if not code.isascii() or len(code) > 100:
                    raise ValueError("Неверный формат кода")
                room_id, _, secret = code.partition(":")
                candidate = self.rooms.get(room_id.upper())
                if (candidate is None or candidate.guest is not None
                        or not secrets.compare_digest(candidate.secret, secret)):
                    raise ValueError("Код неверен, устарел или клиент уже занят")
                room = candidate
                room_id = room_id.upper()
                room.guest = writer
                room.phase = "requested"
                name = str(first.get("name", "Оператор"))[:80]
                await write_packet(room.host, {"type": "request", "name": name})
                await write_packet(writer, {"type": "waiting"})
            else:
                raise ValueError("Ожидалась регистрация или подключение")

            while True:
                # У ожидающего клиента нет лимита; запрос согласия / SDP не зависает навечно.
                timeout = 90 if room.phase in {"requested", "accepted", "offered"} else None
                message = await asyncio.wait_for(read_packet(reader), timeout)
                kind = message["type"]
                other = room.guest if is_host else room.host
                if kind == "bye":
                    break
                if is_host and kind == "decision" and room.phase == "requested":
                    if message.get("allow") is not True:
                        await write_packet(other, {"type": "rejected"})
                        break
                    room.phase = "accepted"
                    await write_packet(other, {"type": "accepted"})
                elif not is_host and kind == "offer" and room.phase == "accepted":
                    if not isinstance(message.get("sdp"), str):
                        raise ValueError("Нет SDP")
                    room.phase = "offered"
                    await write_packet(other, {"type": "offer", "sdp": message["sdp"]})
                elif is_host and kind == "answer" and room.phase == "offered":
                    if not isinstance(message.get("sdp"), str):
                        raise ValueError("Нет SDP")
                    room.phase = "connected"
                    await write_packet(other, {"type": "answer", "sdp": message["sdp"]})
                else:
                    raise ValueError("Сообщение недопустимо на этом этапе")
        except (asyncio.IncompleteReadError, ConnectionError, ssl.SSLError):
            pass
        except (ValueError, TimeoutError) as exc:
            try:
                await write_packet(writer, {"type": "error", "message": str(exc) or "Тайм-аут"})
            except (ConnectionError, TimeoutError):
                pass
        finally:
            # Не удаляем чужую комнату при неудачной попытке join.
            if room is not None and self.rooms.get(room_id) is room:
                del self.rooms[room_id]
                other = room.guest if is_host else room.host
                if other is not None and not other.is_closing():
                    try:
                        await write_packet(other, {"type": "peer_left"})
                    except (ConnectionError, TimeoutError):
                        pass
                    other.close()
            self.connections.discard(writer)
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, ssl.SSLError):
                pass


async def serve():
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(config.SIGNALING_CERT, config.SIGNALING_KEY)
    app = SignalingServer()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((config.SERVER_BIND_IP, config.SIGNALING_PORT))
    listener.setblocking(False)
    server = await asyncio.start_server(app.handle, sock=listener, ssl=context,
                                        ssl_handshake_timeout=10)
    print(f"Signaling TLS: {config.SERVER_BIND_IP}:{config.SIGNALING_PORT}")
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--init-cert", action="store_true")
    args = parser.parse_args()
    if args.init_cert:
        init_certificate()
    else:
        try:
            asyncio.run(serve())
        except KeyboardInterrupt:
            pass
        except FileNotFoundError:
            raise SystemExit("Сначала выполните: python signaling_server.py --init-cert")

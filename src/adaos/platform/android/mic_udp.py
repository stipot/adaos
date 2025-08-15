import socket
from typing import Generator, Optional


def udp_audio_stream(host: str = "127.0.0.1", port: int = 29100, bufsize: int = 8192) -> Generator[bytes, None, None]:
    """
    Слушает UDP порт и выдаёт пришедшие PCM16 чанки.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((host, port))
    try:
        while True:
            data, _ = sock.recvfrom(bufsize)
            if data:
                yield data
    finally:
        sock.close()


class AndroidMicUDP:
    """
    Обёртка, чтобы подложить вместо sounddevice raw stream.
    """

    def __init__(self, host="127.0.0.1", port=29100, bufsize=8192):
        self.host = host
        self.port = port
        self.bufsize = bufsize

    def listen_stream(self):
        return udp_audio_stream(self.host, self.port, self.bufsize)

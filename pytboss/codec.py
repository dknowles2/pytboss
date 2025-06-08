"""Encoding & decoding routines."""

from math import floor
from random import randint

KEY = [0x8F, 0x80, 0x19, 0xCF, 0x77, 0x6C, 0xFE, 0xB7]
PADDING_LEN = 16


def timed_key(uptime: float) -> list[int]:
    """Port of getCodecKey() from the PB firmware."""
    ret = []
    n = floor(max(uptime - 5, 0) / 10)  # 10 second buffer
    key = KEY.copy()
    while len(key) > 1:
        v = key.pop(n % len(key))
        ret.append((v ^ n) & 0xFF)
        n = (n * v + v) & 0xFF
    ret.append(key[0])
    return ret


def encode(data: bytes, *, key: list[int] = KEY) -> bytes:
    """Port of the encoding portion of codec() from the PB firmware."""
    data = bytes([randint(0, 254) for _ in range(PADDING_LEN)]) + b"\xff" + data
    ret = bytearray()
    key = key.copy()
    for i in range(len(data)):
        k = key[i % len(key)]
        m = (data[i] ^ k) & 0xFF
        ret.append(m)
        k2 = (i + 1) % len(key)
        key[k2] = ((key[k2] ^ m) + i) & 0xFF
    return bytes(ret)


def decode(data: bytes, *, key: list[int] = KEY) -> bytes:
    """Port of the decoding portion of codec() from the PB firmware."""
    ret = bytearray()
    key = key.copy()
    for i in range(len(data)):
        k = key[i % len(key)]
        m = (data[i] ^ k) & 0xFF
        ret.append(m)
        k2 = (i + 1) % len(key)
        key[k2] = ((key[k2] ^ data[i]) + i) & 0xFF

    try:
        ret = ret[ret.index(0xFF) + 1 :]
    except ValueError:
        pass

    return bytes(ret)

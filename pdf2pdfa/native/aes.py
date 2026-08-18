"""Small pure-Python AES implementation for PDF Standard Security Handler.

The code implements AES-128/192/256 block encryption/decryption and CBC/ECB
helpers. It exists so encrypted PDF support does not require OpenSSL bindings,
cryptography, PyCryptodome or an external executable.
"""

from __future__ import annotations


class AESError(ValueError):
    pass


_SBOX = (
    0x63,0x7c,0x77,0x7b,0xf2,0x6b,0x6f,0xc5,0x30,0x01,0x67,0x2b,0xfe,0xd7,0xab,0x76,
    0xca,0x82,0xc9,0x7d,0xfa,0x59,0x47,0xf0,0xad,0xd4,0xa2,0xaf,0x9c,0xa4,0x72,0xc0,
    0xb7,0xfd,0x93,0x26,0x36,0x3f,0xf7,0xcc,0x34,0xa5,0xe5,0xf1,0x71,0xd8,0x31,0x15,
    0x04,0xc7,0x23,0xc3,0x18,0x96,0x05,0x9a,0x07,0x12,0x80,0xe2,0xeb,0x27,0xb2,0x75,
    0x09,0x83,0x2c,0x1a,0x1b,0x6e,0x5a,0xa0,0x52,0x3b,0xd6,0xb3,0x29,0xe3,0x2f,0x84,
    0x53,0xd1,0x00,0xed,0x20,0xfc,0xb1,0x5b,0x6a,0xcb,0xbe,0x39,0x4a,0x4c,0x58,0xcf,
    0xd0,0xef,0xaa,0xfb,0x43,0x4d,0x33,0x85,0x45,0xf9,0x02,0x7f,0x50,0x3c,0x9f,0xa8,
    0x51,0xa3,0x40,0x8f,0x92,0x9d,0x38,0xf5,0xbc,0xb6,0xda,0x21,0x10,0xff,0xf3,0xd2,
    0xcd,0x0c,0x13,0xec,0x5f,0x97,0x44,0x17,0xc4,0xa7,0x7e,0x3d,0x64,0x5d,0x19,0x73,
    0x60,0x81,0x4f,0xdc,0x22,0x2a,0x90,0x88,0x46,0xee,0xb8,0x14,0xde,0x5e,0x0b,0xdb,
    0xe0,0x32,0x3a,0x0a,0x49,0x06,0x24,0x5c,0xc2,0xd3,0xac,0x62,0x91,0x95,0xe4,0x79,
    0xe7,0xc8,0x37,0x6d,0x8d,0xd5,0x4e,0xa9,0x6c,0x56,0xf4,0xea,0x65,0x7a,0xae,0x08,
    0xba,0x78,0x25,0x2e,0x1c,0xa6,0xb4,0xc6,0xe8,0xdd,0x74,0x1f,0x4b,0xbd,0x8b,0x8a,
    0x70,0x3e,0xb5,0x66,0x48,0x03,0xf6,0x0e,0x61,0x35,0x57,0xb9,0x86,0xc1,0x1d,0x9e,
    0xe1,0xf8,0x98,0x11,0x69,0xd9,0x8e,0x94,0x9b,0x1e,0x87,0xe9,0xce,0x55,0x28,0xdf,
    0x8c,0xa1,0x89,0x0d,0xbf,0xe6,0x42,0x68,0x41,0x99,0x2d,0x0f,0xb0,0x54,0xbb,0x16,
)
_INV_SBOX = [0] * 256
for _i, _value in enumerate(_SBOX):
    _INV_SBOX[_value] = _i
_INV_SBOX = tuple(_INV_SBOX)

_RCON = (0x00,0x01,0x02,0x04,0x08,0x10,0x20,0x40,0x80,0x1B,0x36,0x6C,0xD8,0xAB,0x4D,0x9A)


def _xtime(value: int) -> int:
    return ((value << 1) ^ (0x1B if value & 0x80 else 0)) & 0xFF


def _mul(a: int, b: int) -> int:
    result = 0
    while b:
        if b & 1:
            result ^= a
        a = _xtime(a)
        b >>= 1
    return result


def _sub_word(word: list[int]) -> list[int]:
    return [_SBOX[value] for value in word]


def _rot_word(word: list[int]) -> list[int]:
    return word[1:] + word[:1]


def _xor(a: bytes, b: bytes) -> bytes:
    return bytes(x ^ y for x, y in zip(a, b))


class AES:
    def __init__(self, key: bytes) -> None:
        if len(key) not in (16, 24, 32):
            raise AESError("AES key must be 16, 24 or 32 bytes")
        self.key = bytes(key)
        self.nk = len(key) // 4
        self.nr = {4: 10, 6: 12, 8: 14}[self.nk]
        self.round_keys = self._expand_key()

    def _expand_key(self) -> tuple[bytes, ...]:
        words: list[list[int]] = [list(self.key[i : i + 4]) for i in range(0, len(self.key), 4)]
        total = 4 * (self.nr + 1)
        for index in range(self.nk, total):
            temp = words[index - 1][:]
            if index % self.nk == 0:
                temp = _sub_word(_rot_word(temp))
                temp[0] ^= _RCON[index // self.nk]
            elif self.nk > 6 and index % self.nk == 4:
                temp = _sub_word(temp)
            words.append([words[index - self.nk][j] ^ temp[j] for j in range(4)])
        keys: list[bytes] = []
        for round_index in range(self.nr + 1):
            block = bytearray()
            for word in words[round_index * 4 : round_index * 4 + 4]:
                block.extend(word)
            keys.append(bytes(block))
        return tuple(keys)

    @staticmethod
    def _add_round_key(state: list[int], key: bytes) -> None:
        for index, value in enumerate(key):
            state[index] ^= value

    @staticmethod
    def _sub_bytes(state: list[int]) -> None:
        for index, value in enumerate(state):
            state[index] = _SBOX[value]

    @staticmethod
    def _inv_sub_bytes(state: list[int]) -> None:
        for index, value in enumerate(state):
            state[index] = _INV_SBOX[value]

    @staticmethod
    def _shift_rows(state: list[int]) -> None:
        # AES state is column-major: index = row + 4*column.
        copy = state[:]
        for row in range(4):
            for column in range(4):
                state[row + 4 * column] = copy[row + 4 * ((column + row) % 4)]

    @staticmethod
    def _inv_shift_rows(state: list[int]) -> None:
        copy = state[:]
        for row in range(4):
            for column in range(4):
                state[row + 4 * column] = copy[row + 4 * ((column - row) % 4)]

    @staticmethod
    def _mix_columns(state: list[int]) -> None:
        for column in range(4):
            i = 4 * column
            a0, a1, a2, a3 = state[i : i + 4]
            state[i] = _mul(a0, 2) ^ _mul(a1, 3) ^ a2 ^ a3
            state[i + 1] = a0 ^ _mul(a1, 2) ^ _mul(a2, 3) ^ a3
            state[i + 2] = a0 ^ a1 ^ _mul(a2, 2) ^ _mul(a3, 3)
            state[i + 3] = _mul(a0, 3) ^ a1 ^ a2 ^ _mul(a3, 2)

    @staticmethod
    def _inv_mix_columns(state: list[int]) -> None:
        for column in range(4):
            i = 4 * column
            a0, a1, a2, a3 = state[i : i + 4]
            state[i] = _mul(a0, 14) ^ _mul(a1, 11) ^ _mul(a2, 13) ^ _mul(a3, 9)
            state[i + 1] = _mul(a0, 9) ^ _mul(a1, 14) ^ _mul(a2, 11) ^ _mul(a3, 13)
            state[i + 2] = _mul(a0, 13) ^ _mul(a1, 9) ^ _mul(a2, 14) ^ _mul(a3, 11)
            state[i + 3] = _mul(a0, 11) ^ _mul(a1, 13) ^ _mul(a2, 9) ^ _mul(a3, 14)

    def encrypt_block(self, block: bytes) -> bytes:
        if len(block) != 16:
            raise AESError("AES block must be 16 bytes")
        state = list(block)
        self._add_round_key(state, self.round_keys[0])
        for round_index in range(1, self.nr):
            self._sub_bytes(state)
            self._shift_rows(state)
            self._mix_columns(state)
            self._add_round_key(state, self.round_keys[round_index])
        self._sub_bytes(state)
        self._shift_rows(state)
        self._add_round_key(state, self.round_keys[self.nr])
        return bytes(state)

    def decrypt_block(self, block: bytes) -> bytes:
        if len(block) != 16:
            raise AESError("AES block must be 16 bytes")
        state = list(block)
        self._add_round_key(state, self.round_keys[self.nr])
        for round_index in range(self.nr - 1, 0, -1):
            self._inv_shift_rows(state)
            self._inv_sub_bytes(state)
            self._add_round_key(state, self.round_keys[round_index])
            self._inv_mix_columns(state)
        self._inv_shift_rows(state)
        self._inv_sub_bytes(state)
        self._add_round_key(state, self.round_keys[0])
        return bytes(state)


def pkcs7_pad(data: bytes, block_size: int = 16) -> bytes:
    if block_size <= 0 or block_size > 255:
        raise ValueError("invalid PKCS#7 block size")
    count = block_size - (len(data) % block_size)
    return data + bytes([count]) * count


def pkcs7_unpad(data: bytes, block_size: int = 16) -> bytes:
    if not data or len(data) % block_size:
        raise AESError("invalid padded AES payload length")
    count = data[-1]
    if count <= 0 or count > block_size or data[-count:] != bytes([count]) * count:
        raise AESError("invalid PKCS#7 padding")
    return data[:-count]


def cbc_encrypt(key: bytes, data: bytes, iv: bytes, *, pad: bool = False) -> bytes:
    if len(iv) != 16:
        raise AESError("AES-CBC IV must be 16 bytes")
    if pad:
        data = pkcs7_pad(data)
    if len(data) % 16:
        raise AESError("AES-CBC input is not block-aligned")
    cipher = AES(key)
    out = bytearray()
    previous = iv
    for offset in range(0, len(data), 16):
        block = _xor(data[offset : offset + 16], previous)
        encrypted = cipher.encrypt_block(block)
        out.extend(encrypted)
        previous = encrypted
    return bytes(out)


def cbc_decrypt(key: bytes, data: bytes, iv: bytes, *, unpad: bool = False) -> bytes:
    if len(iv) != 16:
        raise AESError("AES-CBC IV must be 16 bytes")
    if len(data) % 16:
        raise AESError("AES-CBC ciphertext is not block-aligned")
    cipher = AES(key)
    out = bytearray()
    previous = iv
    for offset in range(0, len(data), 16):
        block = data[offset : offset + 16]
        out.extend(_xor(cipher.decrypt_block(block), previous))
        previous = block
    plaintext = bytes(out)
    return pkcs7_unpad(plaintext) if unpad else plaintext


def ecb_encrypt(key: bytes, data: bytes) -> bytes:
    if len(data) % 16:
        raise AESError("AES-ECB input is not block-aligned")
    cipher = AES(key)
    return b"".join(cipher.encrypt_block(data[offset : offset + 16]) for offset in range(0, len(data), 16))


def ecb_decrypt(key: bytes, data: bytes) -> bytes:
    if len(data) % 16:
        raise AESError("AES-ECB ciphertext is not block-aligned")
    cipher = AES(key)
    return b"".join(cipher.decrypt_block(data[offset : offset + 16]) for offset in range(0, len(data), 16))

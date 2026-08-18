from __future__ import annotations

from pdf2pdfa.native.aes import AES, cbc_decrypt, cbc_encrypt, ecb_decrypt, ecb_encrypt


def _h(value: str) -> bytes:
    return bytes.fromhex(value.replace(" ", "").replace("\n", ""))


def test_aes128_nist_single_block_vector():
    key = _h("000102030405060708090a0b0c0d0e0f")
    plain = _h("00112233445566778899aabbccddeeff")
    cipher = _h("69c4e0d86a7b0430d8cdb78070b4c55a")
    aes = AES(key)
    assert aes.encrypt_block(plain) == cipher
    assert aes.decrypt_block(cipher) == plain


def test_aes192_nist_single_block_vector():
    key = _h("000102030405060708090a0b0c0d0e0f1011121314151617")
    plain = _h("00112233445566778899aabbccddeeff")
    cipher = _h("dda97ca4864cdfe06eaf70a0ec0d7191")
    aes = AES(key)
    assert aes.encrypt_block(plain) == cipher
    assert aes.decrypt_block(cipher) == plain


def test_aes256_nist_single_block_vector():
    key = _h("000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f")
    plain = _h("00112233445566778899aabbccddeeff")
    cipher = _h("8ea2b7ca516745bfeafc49904b496089")
    aes = AES(key)
    assert aes.encrypt_block(plain) == cipher
    assert aes.decrypt_block(cipher) == plain


def test_aes128_cbc_nist_vector():
    key = _h("2b7e151628aed2a6abf7158809cf4f3c")
    iv = _h("000102030405060708090a0b0c0d0e0f")
    plaintext = _h(
        "6bc1bee22e409f96e93d7e117393172a"
        "ae2d8a571e03ac9c9eb76fac45af8e51"
        "30c81c46a35ce411e5fbc1191a0a52ef"
        "f69f2445df4f9b17ad2b417be66c3710"
    )
    ciphertext = _h(
        "7649abac8119b246cee98e9b12e9197d"
        "5086cb9b507219ee95db113a917678b2"
        "73bed6b8e3c1743b7116e69e22229516"
        "3ff1caa1681fac09120eca307586e1a7"
    )
    assert cbc_encrypt(key, plaintext, iv) == ciphertext
    assert cbc_decrypt(key, ciphertext, iv) == plaintext


def test_ecb_helpers_roundtrip_multiple_blocks():
    key = bytes(range(32))
    plaintext = bytes(range(64))
    encrypted = ecb_encrypt(key, plaintext)
    assert encrypted != plaintext
    assert ecb_decrypt(key, encrypted) == plaintext

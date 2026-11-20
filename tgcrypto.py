"""AES-IGE compatibility used by opentele; no native tgcrypto build required."""
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


def _xor(a, b):
    return bytes(x ^ y for x, y in zip(a, b))


def _ige(data, key, iv, decrypt):
    data, key, iv = bytes(data), bytes(key), bytes(iv)
    if len(key) != 32 or len(iv) != 32 or len(data) % 16:
        raise ValueError('AES-256-IGE requires a 32-byte key, 32-byte IV and complete blocks')
    cipher = Cipher(algorithms.AES(key), modes.ECB())
    context = cipher.decryptor() if decrypt else cipher.encryptor()
    previous_cipher, previous_plain = iv[:16], iv[16:]
    result = bytearray()
    for offset in range(0, len(data), 16):
        block = data[offset:offset + 16]
        if decrypt:
            plain = _xor(context.update(_xor(block, previous_plain)), previous_cipher)
            result.extend(plain)
            previous_cipher, previous_plain = block, plain
        else:
            encrypted = _xor(context.update(_xor(block, previous_cipher)), previous_plain)
            result.extend(encrypted)
            previous_cipher, previous_plain = encrypted, block
    context.finalize()
    return bytes(result)


def ige256_encrypt(data, key, iv):
    return _ige(data, key, iv, False)


def ige256_decrypt(data, key, iv):
    return _ige(data, key, iv, True)

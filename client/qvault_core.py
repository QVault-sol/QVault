"""QVault core: Winternitz signatures + instruction builders.

Bit-exact with the on-chain program (program/src/lib.rs).
Run directly to regenerate the test vector program/tests/vector.txt.
"""
from __future__ import annotations

import hashlib
import struct
from pathlib import Path

from solders.instruction import AccountMeta, Instruction
from solders.pubkey import Pubkey

# ───────────── Parameters (must match the program) ─────────────
N = 28
MSG_CHUNKS = N
CS_CHUNKS = 2
L = MSG_CHUNKS + CS_CHUNKS
SIG_LEN = L * N  # 840
W = 255
VAULT_SEED = b"vault"
DOMAIN = b"QVAULT-v2"
FEE_BPS = 10
STATE_LEN = 139

SOL_MINT = Pubkey.default()  # all zeros means SOL
SYSTEM_PROGRAM = Pubkey.from_string("11111111111111111111111111111111")
RENT_SYSVAR = Pubkey.from_string("SysvarRent111111111111111111111111111111111")
TOKEN_PROGRAM = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
TOKEN_2022_PROGRAM = Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
ATA_PROGRAM = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")

STATUS = {0: "empty", 1: "active", 2: "rotated (spent)"}


def _sha(*parts: bytes) -> bytes:
    h = hashlib.sha256()
    for p in parts:
        h.update(p)
    return h.digest()


def chain_step(x: bytes) -> bytes:
    return _sha(x)[:N]


def chunks(digest: bytes) -> list[int]:
    c = list(digest[:MSG_CHUNKS])
    checksum = sum(W - b for b in c)
    return c + [checksum >> 8, checksum & 0xFF]


def message_digest(program_id: Pubkey, vault: Pubkey, recipient: Pubkey,
                   next_vault: Pubkey, mint: Pubkey, amount: int) -> bytes:
    return _sha(DOMAIN, bytes(program_id), bytes(vault), bytes(recipient),
                bytes(next_vault), bytes(mint), struct.pack("<Q", amount))[:N]


def fee_for(amount: int) -> int:
    return amount * FEE_BPS // 10_000


class WotsKey:
    """One-time key number `index`, derived deterministically from a 32-byte seed."""

    def __init__(self, seed: bytes, index: int):
        assert len(seed) == 32
        self.index = index
        self.sk = [_sha(b"QVAULT-SK", seed, struct.pack("<I", index), bytes([i]))[:N]
                   for i in range(L)]
        self._pk_hash: bytes | None = None

    def pk_hash(self) -> bytes:
        if self._pk_hash is None:
            ends = []
            for s in self.sk:
                x = s
                for _ in range(W):
                    x = chain_step(x)
                ends.append(x)
            self._pk_hash = _sha(b"".join(ends))
        return self._pk_hash

    def sign(self, digest: bytes) -> bytes:
        out = []
        for s, c in zip(self.sk, chunks(digest)):
            x = s
            for _ in range(c):
                x = chain_step(x)
            out.append(x)
        return b"".join(out)

    def vault(self, program_id: Pubkey) -> tuple[Pubkey, int]:
        return Pubkey.find_program_address([VAULT_SEED, self.pk_hash()], program_id)


def recover_pk_hash(sig: bytes, digest: bytes) -> bytes:
    ends = []
    for i, c in enumerate(chunks(digest)):
        x = sig[i * N:(i + 1) * N]
        for _ in range(W - c):
            x = chain_step(x)
        ends.append(x)
    return _sha(b"".join(ends))


# ───────────── Decode vault state ─────────────
def decode_state(data: bytes) -> dict:
    if len(data) != STATE_LEN:
        raise ValueError("Not a QVault vault")
    return {
        "status": data[0],
        "bump": data[1],
        "pk_hash": data[2:34],
        "next_vault": Pubkey.from_bytes(data[34:66]),
        "recipient": Pubkey.from_bytes(data[66:98]),
        "mint": Pubkey.from_bytes(data[98:130]),
        "amount": struct.unpack("<Q", data[130:138])[0],
        "paid": data[138] != 0,
    }


# ───────────── Instructions ─────────────
def ix_open(program_id: Pubkey, payer: Pubkey, key: WotsKey) -> Instruction:
    vault, bump = key.vault(program_id)
    return Instruction(program_id, bytes([0]) + key.pk_hash() + bytes([bump]), [
        AccountMeta(payer, True, True),
        AccountMeta(vault, False, True),
        AccountMeta(SYSTEM_PROGRAM, False, False),
        AccountMeta(RENT_SYSVAR, False, False),
    ])


def ix_commit(program_id: Pubkey, vault: Pubkey, recipient: Pubkey, next_vault: Pubkey,
              mint: Pubkey, amount: int, sig: bytes) -> Instruction:
    assert len(sig) == SIG_LEN
    data = bytes([1]) + bytes(mint) + struct.pack("<Q", amount) + sig
    return Instruction(program_id, data, [
        AccountMeta(vault, False, True),
        AccountMeta(recipient, False, False),
        AccountMeta(next_vault, False, False),
    ])


def ix_sweep(program_id: Pubkey, vault: Pubkey, vault_token: Pubkey, mint: Pubkey,
             next_token: Pubkey, recipient_token: Pubkey, treasury_token: Pubkey,
             token_program: Pubkey) -> Instruction:
    return Instruction(program_id, bytes([2]), [
        AccountMeta(vault, False, True),
        AccountMeta(vault_token, False, True),
        AccountMeta(mint, False, False),
        AccountMeta(next_token, False, True),
        AccountMeta(recipient_token, False, True),
        AccountMeta(treasury_token, False, True),
        AccountMeta(token_program, False, False),
    ])


def ix_finish(program_id: Pubkey, vault: Pubkey, next_vault: Pubkey, recipient: Pubkey,
              treasury: Pubkey) -> Instruction:
    return Instruction(program_id, bytes([3]), [
        AccountMeta(vault, False, True),
        AccountMeta(next_vault, False, True),
        AccountMeta(recipient, False, True),
        AccountMeta(treasury, False, True),
        AccountMeta(RENT_SYSVAR, False, False),
    ])


def ata(owner: Pubkey, mint: Pubkey, token_program: Pubkey = TOKEN_PROGRAM) -> Pubkey:
    return Pubkey.find_program_address([bytes(owner), bytes(token_program), bytes(mint)], ATA_PROGRAM)[0]


def ix_create_ata_idempotent(payer: Pubkey, owner: Pubkey, mint: Pubkey,
                             token_program: Pubkey = TOKEN_PROGRAM) -> Instruction:
    return Instruction(ATA_PROGRAM, bytes([1]), [
        AccountMeta(payer, True, True),
        AccountMeta(ata(owner, mint, token_program), False, True),
        AccountMeta(owner, False, False),
        AccountMeta(mint, False, False),
        AccountMeta(SYSTEM_PROGRAM, False, False),
        AccountMeta(token_program, False, False),
    ])


def ix_token_transfer_checked(source: Pubkey, mint: Pubkey, dest: Pubkey, authority: Pubkey,
                              amount: int, decimals: int,
                              token_program: Pubkey = TOKEN_PROGRAM) -> Instruction:
    return Instruction(token_program, bytes([12]) + struct.pack("<Q", amount) + bytes([decimals]), [
        AccountMeta(source, False, True),
        AccountMeta(mint, False, False),
        AccountMeta(dest, False, True),
        AccountMeta(authority, True, False),
    ])


# ───────────── Test vector for the Rust cross-check ─────────────
def write_vector(path: Path) -> None:
    seed = bytes(range(32))
    key = WotsKey(seed, 5)
    program_id = Pubkey(bytes([11] * 32))
    vault, _ = key.vault(program_id)
    recipient = Pubkey(bytes([22] * 32))
    next_vault = Pubkey(bytes([33] * 32))
    mint = TOKEN_PROGRAM  # any 32 bytes
    amount = 123_456_789
    digest = message_digest(program_id, vault, recipient, next_vault, mint, amount)
    sig = key.sign(digest)
    assert recover_pk_hash(sig, digest) == key.pk_hash()
    lines = {
        "seed": seed.hex(), "index": "5", "program_id": bytes(program_id).hex(),
        "vault": bytes(vault).hex(), "recipient": bytes(recipient).hex(),
        "next_vault": bytes(next_vault).hex(), "mint": bytes(mint).hex(),
        "amount": str(amount), "digest": digest.hex(), "pk_hash": key.pk_hash().hex(),
        "sig": sig.hex(),
    }
    path.write_text("".join(f"{k} = {v}\n" for k, v in lines.items()))
    print(f"Test vector written: {path}")


if __name__ == "__main__":
    write_vector(Path(__file__).resolve().parent.parent / "program" / "tests" / "vector.txt")

// QVault core for the browser: Winternitz one-time signatures + instruction builders.
// Bit-exact with program/src/lib.rs and client/qvault_core.py (checked by test/core.test.js).

import { sha256 } from "@noble/hashes/sha2";
import { Buffer } from "buffer";
import {
  PublicKey,
  SystemProgram,
  SYSVAR_RENT_PUBKEY,
  TransactionInstruction,
} from "@solana/web3.js";

export const N = 28;
export const MSG_CHUNKS = N;
export const L = MSG_CHUNKS + 2;
export const SIG_LEN = L * N; // 840
export const W = 255;
export const FEE_BPS = 10n;
export const STATE_LEN = 139;

const enc = new TextEncoder();
const VAULT_SEED = enc.encode("vault");
const DOMAIN = enc.encode("QVAULT-v2");
const SK_TAG = enc.encode("QVAULT-SK");

export const SOL_MINT = new PublicKey(new Uint8Array(32));
export const TOKEN_PROGRAM = new PublicKey("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA");
export const TOKEN_2022_PROGRAM = new PublicKey("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb");
export const ATA_PROGRAM = new PublicKey("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL");

export const STATUS = { 1: "active", 2: "rotated" };

function concat(...parts) {
  const len = parts.reduce((n, p) => n + p.length, 0);
  const out = new Uint8Array(len);
  let o = 0;
  for (const p of parts) {
    out.set(p, o);
    o += p.length;
  }
  return out;
}

const u32le = (v) => {
  const b = new Uint8Array(4);
  new DataView(b.buffer).setUint32(0, v, true);
  return b;
};
const u64le = (v) => {
  const b = new Uint8Array(8);
  new DataView(b.buffer).setBigUint64(0, BigInt(v), true);
  return b;
};

export const chainStep = (x) => sha256(x).subarray(0, N);

export function chunks(digest) {
  const c = Array.from(digest.subarray(0, MSG_CHUNKS));
  const checksum = c.reduce((s, b) => s + (W - b), 0);
  return [...c, checksum >> 8, checksum & 0xff];
}

export function messageDigest(programId, vault, recipient, nextVault, mint, amount) {
  return sha256(
    concat(
      DOMAIN,
      programId.toBytes(),
      vault.toBytes(),
      recipient.toBytes(),
      nextVault.toBytes(),
      mint.toBytes(),
      u64le(amount),
    ),
  ).subarray(0, N);
}

export const feeFor = (amount) => (BigInt(amount) * FEE_BPS) / 10000n;

/** One-time key number `index`, derived deterministically from a 32-byte seed. */
export class WotsKey {
  constructor(seed, index) {
    if (seed.length !== 32) throw new Error("seed must be 32 bytes");
    this.index = index;
    this.sk = [];
    for (let i = 0; i < L; i++) {
      this.sk.push(sha256(concat(SK_TAG, seed, u32le(index), Uint8Array.of(i))).subarray(0, N));
    }
    this._pk = null;
  }

  pkHash() {
    if (!this._pk) {
      const ends = [];
      for (const s of this.sk) {
        let x = s;
        for (let j = 0; j < W; j++) x = chainStep(x);
        ends.push(x);
      }
      this._pk = sha256(concat(...ends));
    }
    return this._pk;
  }

  sign(digest) {
    const c = chunks(digest);
    return concat(
      ...this.sk.map((s, i) => {
        let x = s;
        for (let j = 0; j < c[i]; j++) x = chainStep(x);
        return x;
      }),
    );
  }

  vault(programId) {
    return PublicKey.findProgramAddressSync([VAULT_SEED, this.pkHash()], programId);
  }
}

export function recoverPkHash(sig, digest) {
  const c = chunks(digest);
  const ends = [];
  for (let i = 0; i < L; i++) {
    let x = sig.subarray(i * N, (i + 1) * N);
    for (let j = c[i]; j < W; j++) x = chainStep(x);
    ends.push(x);
  }
  return sha256(concat(...ends));
}

export function decodeState(data) {
  if (data.length !== STATE_LEN) throw new Error("Not a QVault vault");
  const dv = new DataView(data.buffer, data.byteOffset, data.byteLength);
  const pk = (o) => new PublicKey(data.subarray(o, o + 32));
  return {
    status: data[0],
    bump: data[1],
    pkHash: data.subarray(2, 34),
    nextVault: pk(34),
    recipient: pk(66),
    mint: pk(98),
    amount: dv.getBigUint64(130, true),
    paid: data[138] !== 0,
  };
}

const meta = (pubkey, isSigner, isWritable) => ({ pubkey, isSigner, isWritable });
const ix = (programId, keys, data) =>
  new TransactionInstruction({ programId, keys, data: Buffer.from(data) });

export function ixOpen(programId, payer, key) {
  const [vault, bump] = key.vault(programId);
  return ix(
    programId,
    [meta(payer, true, true), meta(vault, false, true), meta(SystemProgram.programId, false, false),
      meta(SYSVAR_RENT_PUBKEY, false, false)],
    concat(Uint8Array.of(0), key.pkHash(), Uint8Array.of(bump)),
  );
}

export function ixCommit(programId, vault, recipient, nextVault, mint, amount, sig) {
  if (sig.length !== SIG_LEN) throw new Error("bad signature length");
  return ix(
    programId,
    [meta(vault, false, true), meta(recipient, false, false), meta(nextVault, false, false)],
    concat(Uint8Array.of(1), mint.toBytes(), u64le(amount), sig),
  );
}

export function ixSweep(programId, vault, vaultToken, mint, nextToken, recipientToken, treasuryToken, tokenProgram) {
  return ix(
    programId,
    [meta(vault, false, true), meta(vaultToken, false, true), meta(mint, false, false),
      meta(nextToken, false, true), meta(recipientToken, false, true), meta(treasuryToken, false, true),
      meta(tokenProgram, false, false)],
    Uint8Array.of(2),
  );
}

export function ixFinish(programId, vault, nextVault, recipient, treasury) {
  return ix(
    programId,
    [meta(vault, false, true), meta(nextVault, false, true), meta(recipient, false, true),
      meta(treasury, false, true), meta(SYSVAR_RENT_PUBKEY, false, false)],
    Uint8Array.of(3),
  );
}

export const ata = (owner, mint, tokenProgram = TOKEN_PROGRAM) =>
  PublicKey.findProgramAddressSync([owner.toBytes(), tokenProgram.toBytes(), mint.toBytes()], ATA_PROGRAM)[0];

export function ixCreateAtaIdempotent(payer, owner, mint, tokenProgram = TOKEN_PROGRAM) {
  return ix(
    ATA_PROGRAM,
    [meta(payer, true, true), meta(ata(owner, mint, tokenProgram), false, true), meta(owner, false, false),
      meta(mint, false, false), meta(SystemProgram.programId, false, false), meta(tokenProgram, false, false)],
    Uint8Array.of(1),
  );
}

export function ixTransferChecked(source, mint, dest, authority, amount, decimals, tokenProgram = TOKEN_PROGRAM) {
  return ix(
    tokenProgram,
    [meta(source, false, true), meta(mint, false, false), meta(dest, false, true), meta(authority, true, false)],
    concat(Uint8Array.of(12), u64le(amount), Uint8Array.of(decimals)),
  );
}

export function ixInitMint2(mint, authority, decimals) {
  return ix(TOKEN_PROGRAM, [meta(mint, false, true)],
    concat(Uint8Array.of(20, decimals), authority.toBytes(), Uint8Array.of(0), new Uint8Array(32)));
}

export function ixMintTo(mint, dest, authority, amount) {
  return ix(TOKEN_PROGRAM, [meta(mint, false, true), meta(dest, false, true), meta(authority, true, false)],
    concat(Uint8Array.of(7), u64le(amount)));
}

export const toHex = (b) => Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
export function fromHex(h) {
  const s = h.trim().toLowerCase();
  if (!/^[0-9a-f]{64}$/.test(s)) throw new Error("A recovery code is 64 hex characters");
  return Uint8Array.from(s.match(/../g), (x) => parseInt(x, 16));
}

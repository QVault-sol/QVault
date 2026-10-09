// Cross-checks the browser core against the vector produced by client/qvault_core.py.
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { PublicKey } from "@solana/web3.js";
import * as qc from "../src/core.js";

const path = fileURLToPath(new URL("../../program/tests/vector.txt", import.meta.url));
const v = Object.fromEntries(
  readFileSync(path, "utf8").trim().split("\n").map((l) => l.split(" = ").map((s) => s.trim())),
);
const pk = (h) => new PublicKey(qc.fromHex(h));
const hexEq = (a, b, what) => {
  if (qc.toHex(a) !== b) throw new Error(`${what} mismatch`);
};

const key = new qc.WotsKey(qc.fromHex(v.seed), Number(v.index));
const digest = qc.messageDigest(pk(v.program_id), pk(v.vault), pk(v.recipient), pk(v.next_vault), pk(v.mint), BigInt(v.amount));
hexEq(digest, v.digest, "digest");
hexEq(key.pkHash(), v.pk_hash, "pk_hash");
hexEq(key.sign(digest), v.sig, "signature");
hexEq(qc.recoverPkHash(key.sign(digest), digest), v.pk_hash, "recovered pk_hash");
const [vault] = key.vault(pk(v.program_id));
hexEq(vault.toBytes(), v.vault, "vault PDA");
if (qc.feeFor(500_000_000n) !== 500_000n) throw new Error("fee");
console.log("web core: all checks passed");

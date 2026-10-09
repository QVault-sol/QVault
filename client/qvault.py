#!/usr/bin/env python3
"""QVault – quantum-safe vault for SOL and SPL tokens (command line).

Commands
  init                         create a new vault wallet (write down the recovery code!)
  recover <code>               rebuild the wallet from its recovery code
  address                      show the current vault address
  status                       balances (SOL + tokens) of the current vault
  deposit <amount> [--token MINT]          move funds from your normal wallet into the vault
  send <recipient> <amount> [--token MINT] quantum-safe withdrawal
  airdrop [sol]                free devnet SOL for your normal wallet

Only dependency: `solders` (pip install solders).
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.request
from decimal import Decimal
from pathlib import Path

from solders.compute_budget import set_compute_unit_limit
from solders.hash import Hash
from solders.keypair import Keypair
from solders.message import Message
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
from solders.transaction import Transaction

import qvault_core as qc

HOME = Path(os.environ.get("QVAULT_HOME", Path.home() / ".qvault"))
WALLET = HOME / "wallet.json"
LAMPORTS = 1_000_000_000
MAX_TX = 1232
COMMIT_CU = 1_400_000


# ───────────────────────── Helpers ─────────────────────────
def die(msg: str) -> None:
    print(f"✖ {msg}", file=sys.stderr)
    sys.exit(1)


def load_wallet() -> dict:
    if not WALLET.exists():
        die(f"No wallet found ({WALLET}). Run first: python3 qvault.py init --program-id <ID> --treasury <ADDR>")
    return json.loads(WALLET.read_text())


def save_wallet(w: dict) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    tmp = WALLET.with_suffix(".tmp")
    tmp.write_text(json.dumps(w, indent=2))
    os.chmod(tmp, 0o600)
    tmp.replace(WALLET)


def load_payer(path: str) -> Keypair:
    p = Path(os.path.expanduser(path))
    if not p.exists():
        die(f"Fee-payer wallet {p} not found. Create one with: solana-keygen new")
    return Keypair.from_bytes(bytes(json.loads(p.read_text())))


def to_base_units(amount: str, decimals: int) -> int:
    v = Decimal(amount) * (Decimal(10) ** decimals)
    if v != v.to_integral_value() or v <= 0:
        die(f"Invalid amount: {amount}")
    return int(v)


def fmt(units: int, decimals: int) -> str:
    return f"{Decimal(units) / (Decimal(10) ** decimals):f}"


class Rpc:
    def __init__(self, url: str):
        self.url = url
        self._id = 0

    def call(self, method: str, params: list):
        self._id += 1
        body = json.dumps({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}).encode()
        req = urllib.request.Request(self.url, body, {"Content-Type": "application/json"})
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    res = json.loads(r.read())
                break
            except Exception as e:  # network / rate limit
                if attempt == 3:
                    die(f"RPC unreachable ({method}): {e}")
                time.sleep(1.5 * (attempt + 1))
        if "error" in res:
            raise RuntimeError(f"{method}: {res['error'].get('message')} {res['error'].get('data', '')}")
        return res["result"]

    def explorer(self, sig: str) -> str:
        cluster = next((c for c in ("devnet", "testnet") if c in self.url), None)
        if cluster is None and "mainnet" not in self.url:
            return sig
        suffix = f"?cluster={cluster}" if cluster else ""
        return f"https://explorer.solana.com/tx/{sig}{suffix}"

    def account(self, pk: Pubkey) -> dict | None:
        v = self.call("getAccountInfo", [str(pk), {"encoding": "base64", "commitment": "confirmed"}])["value"]
        if v is None:
            return None
        return {"lamports": v["lamports"], "owner": Pubkey.from_string(v["owner"]),
                "data": base64.b64decode(v["data"][0])}

    def balance(self, pk: Pubkey) -> int:
        return self.call("getBalance", [str(pk), {"commitment": "confirmed"}])["value"]

    def blockhash(self) -> Hash:
        return Hash.from_string(self.call("getLatestBlockhash", [{"commitment": "confirmed"}])["value"]["blockhash"])

    def rent(self, size: int) -> int:
        return self.call("getMinimumBalanceForRentExemption", [size])

    def token_accounts(self, owner: Pubkey) -> list[dict]:
        out = []
        for prog in (qc.TOKEN_PROGRAM, qc.TOKEN_2022_PROGRAM):
            res = self.call("getTokenAccountsByOwner", [
                str(owner), {"programId": str(prog)}, {"encoding": "jsonParsed", "commitment": "confirmed"}])
            for it in res["value"]:
                info = it["account"]["data"]["parsed"]["info"]
                out.append({
                    "address": Pubkey.from_string(it["pubkey"]),
                    "mint": Pubkey.from_string(info["mint"]),
                    "amount": int(info["tokenAmount"]["amount"]),
                    "decimals": int(info["tokenAmount"]["decimals"]),
                    "program": prog,
                })
        return out

    def send(self, payer: Keypair, ixs: list, label: str) -> str:
        msg = Message.new_with_blockhash(ixs, payer.pubkey(), self.blockhash())
        tx = Transaction([payer], msg, msg.recent_blockhash)
        raw = bytes(tx)
        if len(raw) > MAX_TX:
            die(f"Transaction too large ({len(raw)} > {MAX_TX} bytes)")
        sig = self.call("sendTransaction", [base64.b64encode(raw).decode(),
                                            {"encoding": "base64", "preflightCommitment": "confirmed"}])
        for _ in range(60):
            st = self.call("getSignatureStatuses", [[sig]])["value"][0]
            if st and st.get("err"):
                raise RuntimeError(f"{label} failed: {st['err']}")
            if st and st.get("confirmationStatus") in ("confirmed", "finalized"):
                print(f"  ✔ {label}  {self.explorer(sig)}")
                return sig
            time.sleep(1)
        raise RuntimeError(f"{label}: not confirmed after 60 s – check `status` later")


def ctx():
    w = load_wallet()
    rpc = Rpc(w["rpc"])
    payer = load_payer(w["keypair"])
    pid = Pubkey.from_string(w["program_id"])
    treasury = Pubkey.from_string(w["treasury"])
    seed = bytes.fromhex(w["seed"])
    return w, rpc, payer, pid, treasury, seed


def vault_state(rpc: Rpc, pid: Pubkey, vault: Pubkey) -> dict | None:
    acc = rpc.account(vault)
    if acc is None or acc["owner"] != pid:
        return None
    st = qc.decode_state(acc["data"])
    st["lamports"] = acc["lamports"]
    return st


def ensure_open(rpc: Rpc, payer: Keypair, pid: Pubkey, key: qc.WotsKey, label: str) -> Pubkey:
    vault, _ = key.vault(pid)
    if vault_state(rpc, pid, vault) is None:
        rpc.send(payer, [qc.ix_open(pid, payer.pubkey(), key)], label)
    return vault


SAFE_2022_EXTENSIONS = {"metadataPointer", "tokenMetadata", "groupPointer", "groupMemberPointer",
                        "tokenGroup", "tokenGroupMember", "mintCloseAuthority", "immutableOwner"}


def assert_supported_mint(rpc: Rpc, mint: Pubkey, prog: Pubkey) -> None:
    """Token-2022 extensions that change transfer behaviour would get stuck in a rotating vault."""
    if prog != qc.TOKEN_2022_PROGRAM:
        return
    v = rpc.call("getAccountInfo", [str(mint), {"encoding": "jsonParsed", "commitment": "confirmed"}])["value"]
    exts = (v or {}).get("data", {}).get("parsed", {}).get("info", {}).get("extensions", [])
    bad = [e["extension"] for e in exts if e.get("extension") not in SAFE_2022_EXTENSIONS]
    if bad:
        die(f"This Token-2022 mint uses {', '.join(bad)}, which QVault does not support yet. Deposit refused.")


def mint_info(rpc: Rpc, mint: Pubkey) -> tuple[int, Pubkey]:
    acc = rpc.account(mint)
    if acc is None or acc["owner"] not in (qc.TOKEN_PROGRAM, qc.TOKEN_2022_PROGRAM):
        die(f"{mint} is not a token mint")
    return acc["data"][44], acc["owner"]


# ───────────────────────── Commands ─────────────────────────
def cmd_init(a) -> None:
    if WALLET.exists() and not a.force:
        die(f"{WALLET} already exists (use --force to overwrite – careful, you can lose funds!)")
    seed = a.seed_hex and bytes.fromhex(a.seed_hex) or os.urandom(32)
    w = {"seed": seed.hex(), "index": 0, "program_id": a.program_id, "treasury": a.treasury,
         "rpc": a.rpc, "keypair": a.keypair, "pending": None}
    save_wallet(w)
    _, rpc, payer, pid, _, _ = ctx()
    if a.seed_hex:
        # Recovery: find the first vault that is not yet spent
        i = 0
        while True:
            st = vault_state(rpc, pid, qc.WotsKey(seed, i).vault(pid)[0])
            if st is None or st["status"] == 1:
                break
            i += 1
        w["index"] = i
        save_wallet(w)
        print(f"✔ Recovered – current vault is #{i}")
    vault = ensure_open(rpc, payer, pid, qc.WotsKey(seed, w["index"]), "Vault opened")
    print("\n══════════════ RECOVERY CODE (write it down offline!) ══════════════")
    print("<hidden in CI>" if os.environ.get("CI") else seed.hex())
    print("═══════════════════════════════════════════════════════════════════")
    print("Whoever has this code controls the vault. It is never transmitted.\n")
    print(f"Vault address: {vault}")


def cmd_address(a) -> None:
    w, _, _, pid, _, seed = ctx()
    print(qc.WotsKey(seed, w["index"]).vault(pid)[0])


def cmd_status(a) -> None:
    w, rpc, payer, pid, _, seed = ctx()
    vault, _ = qc.WotsKey(seed, w["index"]).vault(pid)
    st = vault_state(rpc, pid, vault)
    print(f"Vault #{w['index']}: {vault}")
    if st is None:
        print("  (not opened yet – run `init` again)")
        return
    keep = rpc.rent(qc.STATE_LEN)
    print(f"  State : {qc.STATUS.get(st['status'], '?')}")
    print(f"  SOL   : {fmt(st['lamports'] - keep, 9)}")
    for t in rpc.token_accounts(vault):
        print(f"  Token : {fmt(t['amount'], t['decimals'])}  (mint {t['mint']})")
    print(f"Fee payer {payer.pubkey()}: {fmt(rpc.balance(payer.pubkey()), 9)} SOL")
    if w.get("pending"):
        print("⚠ Pending withdrawal – repeat `send` with exactly these values:", w["pending"])


def cmd_deposit(a) -> None:
    w, rpc, payer, pid, _, seed = ctx()
    vault, _ = qc.WotsKey(seed, w["index"]).vault(pid)
    if vault_state(rpc, pid, vault) is None:
        die("Vault not opened")
    if a.token is None:
        lam = to_base_units(a.amount, 9)
        rpc.send(payer, [transfer(TransferParams(from_pubkey=payer.pubkey(), to_pubkey=vault, lamports=lam))],
                 f"Deposited {a.amount} SOL")
        return
    mint = Pubkey.from_string(a.token)
    dec, prog = mint_info(rpc, mint)
    assert_supported_mint(rpc, mint, prog)
    units = to_base_units(a.amount, dec)
    src = qc.ata(payer.pubkey(), mint, prog)
    rpc.send(payer, [
        qc.ix_create_ata_idempotent(payer.pubkey(), vault, mint, prog),
        qc.ix_token_transfer_checked(src, mint, qc.ata(vault, mint, prog), payer.pubkey(), units, dec, prog),
    ], f"Deposited {a.amount} tokens")


def cmd_send(a) -> None:
    w, rpc, payer, pid, treasury, seed = ctx()
    idx = w["index"]
    key, next_key = qc.WotsKey(seed, idx), qc.WotsKey(seed, idx + 1)
    vault, _ = key.vault(pid)
    next_vault, _ = next_key.vault(pid)
    st = vault_state(rpc, pid, vault)
    if st is None:
        die("Vault not opened")

    if st["status"] == 2:
        print("Withdrawal already committed on-chain – resuming.")
    else:
        recipient = Pubkey.from_string(a.recipient)
        if a.token is None:
            mint, dec = qc.SOL_MINT, 9
        else:
            mint = Pubkey.from_string(a.token)
            dec, _ = mint_info(rpc, mint)
        amount = to_base_units(a.amount, dec)
        fee = qc.fee_for(amount)
        request = {"index": idx, "recipient": str(recipient), "mint": str(mint), "amount": amount}

        pend = w.get("pending")
        if pend and pend != request:
            die("There is a signed but unconfirmed withdrawal with different values:\n"
                f"  {pend}\nThis key must not sign twice. Repeat `send` with exactly those values.")

        # Check funds BEFORE signing
        if mint == qc.SOL_MINT:
            excess = st["lamports"] - rpc.rent(qc.STATE_LEN)
            if excess < amount + fee:
                die(f"Not enough SOL in the vault: {fmt(excess, 9)} < {fmt(amount + fee, 9)} (incl. 0.1 % fee)")
            rcpt_acc = rpc.call("getAccountInfo", [str(recipient), {"encoding": "base64"}])["value"]
            if rcpt_acc and rcpt_acc.get("executable"):
                die("That address is a program and cannot receive SOL")
            if rcpt_acc is None and amount < rpc.rent(0):
                die("Recipient account does not exist yet – minimum amount is ~0.00089 SOL")
        else:
            bal = sum(t["amount"] for t in rpc.token_accounts(vault) if t["mint"] == mint)
            if bal < amount + fee:
                die(f"Not enough tokens in the vault: {fmt(bal, dec)} < {fmt(amount + fee, dec)} (incl. 0.1 % fee)")

        print(f"Sending {fmt(amount, dec)} to {recipient}  (fee {fmt(fee, dec)})")
        ensure_open(rpc, payer, pid, next_key, f"Next vault #{idx + 1} opened")

        sig = key.sign(qc.message_digest(pid, vault, recipient, next_vault, mint, amount))
        w["pending"] = request
        save_wallet(w)  # persist before sending: from now on this key counts as used
        try:
            rpc.send(payer, [set_compute_unit_limit(COMMIT_CU),
                             qc.ix_commit(pid, vault, recipient, next_vault, mint, amount, sig)],
                     "Quantum-safe commit (Winternitz)")
        except RuntimeError as e:
            if str(e).startswith("sendTransaction:"):  # rejected in preflight: never broadcast
                w["pending"] = None
                save_wallet(w)
            raise
        st = vault_state(rpc, pid, vault)

    # No signature needed from here on: sweep tokens, then SOL
    rcpt, mint, paid = st["recipient"], st["mint"], st["paid"]
    for t in rpc.token_accounts(vault):
        prog = t["program"]
        next_tok = qc.ata(st["next_vault"], t["mint"], prog)
        ixs = [qc.ix_create_ata_idempotent(payer.pubkey(), st["next_vault"], t["mint"], prog)]
        r_tok = tr_tok = next_tok
        if t["mint"] == mint and not paid:
            r_tok, tr_tok = qc.ata(rcpt, mint, prog), qc.ata(treasury, mint, prog)
            ixs += [qc.ix_create_ata_idempotent(payer.pubkey(), rcpt, mint, prog),
                    qc.ix_create_ata_idempotent(payer.pubkey(), treasury, mint, prog)]
        ixs.append(qc.ix_sweep(pid, vault, t["address"], t["mint"], next_tok, r_tok, tr_tok, prog))
        rpc.send(payer, ixs, f"Token {str(t['mint'])[:8]}… swept")

    rpc.send(payer, [qc.ix_finish(pid, vault, st["next_vault"], rcpt, treasury)], "SOL swept")

    w["index"] = idx + 1
    w["pending"] = None
    save_wallet(w)
    print(f"✔ Done. New vault #{idx + 1}: {st['next_vault']}")


def cmd_airdrop(a) -> None:
    w, rpc, payer, *_ = ctx()
    sig = rpc.call("requestAirdrop", [str(payer.pubkey()), int(Decimal(a.sol) * LAMPORTS)])
    print(f"Airdrop requested ({sig[:20]}…). If it is refused, use https://faucet.solana.com")


def main() -> None:
    p = argparse.ArgumentParser(description="QVault – quantum-safe Solana vault")
    sub = p.add_subparsers(dest="cmd", required=True)

    def wallet_args(s):
        s.add_argument("--program-id", required=True)
        s.add_argument("--treasury", required=True, help="fee wallet (must match the deployed program)")
        s.add_argument("--rpc", default="https://api.devnet.solana.com")
        s.add_argument("--keypair", default="~/.config/solana/id.json", help="normal wallet that pays network fees")
        s.add_argument("--force", action="store_true")

    s = sub.add_parser("init", help="create a new vault wallet")
    wallet_args(s)
    s.set_defaults(fn=cmd_init, seed_hex=None)

    s = sub.add_parser("recover", help="restore from a recovery code")
    s.add_argument("seed_hex")
    wallet_args(s)
    s.set_defaults(fn=cmd_init)

    sub.add_parser("address", help="show the current vault address").set_defaults(fn=cmd_address)
    sub.add_parser("status", help="show balances").set_defaults(fn=cmd_status)

    s = sub.add_parser("deposit", help="move funds into the vault")
    s.add_argument("amount")
    s.add_argument("--token", help="mint address; omit for SOL")
    s.set_defaults(fn=cmd_deposit)

    s = sub.add_parser("send", help="quantum-safe withdrawal")
    s.add_argument("recipient")
    s.add_argument("amount")
    s.add_argument("--token", help="mint address; omit for SOL")
    s.set_defaults(fn=cmd_send)

    s = sub.add_parser("airdrop", help="free devnet SOL")
    s.add_argument("sol", nargs="?", default="2")
    s.set_defaults(fn=cmd_airdrop)

    a = p.parse_args()
    try:
        a.fn(a)
    except RuntimeError as e:
        die(str(e))


if __name__ == "__main__":
    main()

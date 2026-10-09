#!/usr/bin/env python3
"""Create a throw-away SPL test token on devnet and mint some to the fee-payer wallet.

Usage: python3 make_test_token.py [--keypair PATH] [--rpc URL] [--amount 1000]
Prints the new mint address on the last line.
"""
from __future__ import annotations

import argparse
import base64
import struct
import time

from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import Message
from solders.system_program import CreateAccountParams, create_account
from solders.transaction import Transaction

import qvault_core as qc
from qvault import Rpc, load_payer

DECIMALS = 6
MINT_LEN = 82


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--keypair", default="~/.config/solana/id.json")
    p.add_argument("--rpc", default="https://api.devnet.solana.com")
    p.add_argument("--amount", type=int, default=1000)
    a = p.parse_args()

    rpc = Rpc(a.rpc)
    payer = load_payer(a.keypair)
    mint = Keypair()
    me = payer.pubkey()

    init_mint = Instruction(
        qc.TOKEN_PROGRAM,
        bytes([20, DECIMALS]) + bytes(me) + bytes([0]) + bytes(32),  # InitializeMint2, no freeze authority
        [AccountMeta(mint.pubkey(), False, True)],
    )
    mint_to = Instruction(
        qc.TOKEN_PROGRAM,
        bytes([7]) + struct.pack("<Q", a.amount * 10**DECIMALS),  # MintTo
        [AccountMeta(mint.pubkey(), False, True),
         AccountMeta(qc.ata(me, mint.pubkey()), False, True),
         AccountMeta(me, True, False)],
    )
    ixs = [
        create_account(CreateAccountParams(from_pubkey=me, to_pubkey=mint.pubkey(),
                                           lamports=rpc.rent(MINT_LEN), space=MINT_LEN,
                                           owner=qc.TOKEN_PROGRAM)),
        init_mint,
        qc.ix_create_ata_idempotent(me, me, mint.pubkey()),
        mint_to,
    ]
    msg = Message.new_with_blockhash(ixs, me, rpc.blockhash())
    tx = Transaction([payer, mint], msg, msg.recent_blockhash)
    sig = rpc.call("sendTransaction", [base64.b64encode(bytes(tx)).decode(),
                                       {"encoding": "base64", "preflightCommitment": "confirmed"}])
    for _ in range(60):
        st = rpc.call("getSignatureStatuses", [[sig]])["value"][0]
        if st and st.get("err"):
            raise SystemExit(f"Token creation failed: {st['err']}")
        if st and st.get("confirmationStatus") in ("confirmed", "finalized"):
            break
        time.sleep(1)
    print(f"Created test token with {a.amount} units: {rpc.explorer(sig)}")
    print(mint.pubkey())


if __name__ == "__main__":
    main()

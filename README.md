# QVault

[![CI](https://github.com/QVault-sol/QVault/actions/workflows/ci.yml/badge.svg)](https://github.com/QVault-sol/QVault/actions/workflows/ci.yml)

**A quantum-safe vault for SOL and SPL tokens on Solana.**

Every normal Solana wallet is protected by an Ed25519 key, and a large enough
quantum computer could break it. On Solana the address *is* the public key, so
every account is exposed by default.

QVault protects funds with **Winternitz one-time signatures** instead. They rely
only on SHA-256 and are considered secure against quantum computers. Hash-based
vaults already exist for SOL; QVault also covers **USDC and any SPL / Token-2022
token**.

> ⚠️ **Status: devnet prototype, not audited.** Do not store real funds.

**▶ Try it in your browser: [qvault-sol.github.io/QVault](https://qvault-sol.github.io/QVault/)**
Works with Phantom (devnet) or a built-in demo wallet. Create a vault, deposit SOL or
test tokens, and send them with a hash-based signature computed in your browser.

| | Normal wallet | QVault |
|---|---|---|
| Signature | Ed25519 (quantum-vulnerable) | Winternitz / SHA-256 (quantum-safe) |
| SOL | ✔ | ✔ |
| USDC & other SPL tokens | ✔ | ✔ |

---

## How it works

1. **Vault** – an account (PDA) that only the QVault program can move. Its
   address is derived from the hash of your Winternitz public key.
2. **Deposit** – send SOL or tokens to the vault address.
3. **Withdraw** in two phases:
   - **Commit** – one one-time signature fixes *who* receives *how much* of
     *which* token, and *where* everything else goes (the next vault, with a
     fresh key).
   - **Sweep / Finish** – no signature. Each token and the SOL are split
     between recipient, fee wallet and next vault. The destinations are already
     fixed and cannot be changed, so anyone may execute these steps.
4. Every key signs exactly **once**. All keys are derived from a single
   32-byte **recovery code** that you keep offline.

> The network fee (5000 lamports) is still paid by a normal wallet. That wallet
> only needs pocket change; your funds sit in the vault.

### Signature scheme

| Parameter | Value |
|---|---|
| Scheme | Winternitz OTS, w = 256 |
| Chain hash | SHA-256 truncated to 224 bits |
| Chains | 28 message + 2 checksum = 30 |
| Signature size | 840 bytes |
| Signed message | domain ‖ program ‖ vault ‖ recipient ‖ next vault ‖ mint ‖ amount |

---

## Repository layout

```
qvault/
├── program/                 Solana program (Rust)
│   ├── src/lib.rs           Open / Commit / SweepToken / Finish
│   └── tests/e2e.rs         End-to-end tests incl. attack scenarios
├── web/                     Browser app (Phantom or demo wallet), deployed to GitHub Pages
│   ├── src/core.js          Winternitz signing in JS (cross-checked against Rust/Python)
│   └── test/e2e_devnet.py   Browser test of the full flow on devnet
├── client/
│   ├── qvault_core.py       Winternitz crypto + instructions (bit-exact with Rust)
│   ├── qvault.py            CLI: init, deposit, send, status …
│   └── make_test_token.py   creates a throw-away SPL token on devnet
├── .github/workflows/
│   ├── ci.yml               tests + SBF build on every push
│   └── devnet-demo.yml      one-click devnet deploy + SOL/token round trip
└── README.md
```

---

## Quick start on devnet

Devnet SOL is free test money. Commands are for macOS / Linux (on Windows use WSL).

### 1. Install tools

```bash
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh     # Rust
sh -c "$(curl -sSfL https://release.anza.xyz/stable/install)"     # Solana CLI
pip3 install solders                                               # Python client
```

Restart the terminal, then check:

```bash
solana --version
cargo build-sbf --version
```

### 2. Devnet wallet + free SOL

```bash
solana config set --url devnet
solana-keygen new
solana address
```

Paste the address into https://faucet.solana.com, choose **Devnet** and request
SOL. About 3 SOL is enough to deploy.

### 3. Set the fee wallet

In `program/src/lib.rs`, replace the `TREASURY` address with your own wallet.
`FEE_BPS` sets the fee (10 = 0.1 %).

### 4. Test and build

```bash
cd program
cargo test        # all tests must pass
cargo build-sbf   # produces target/deploy/qvault.so
```

### 5. Deploy

```bash
solana program deploy target/deploy/qvault.so
```

Copy the `Program Id` from the output.

### 6. Create a vault

```bash
cd ../client
python3 qvault.py init --program-id <PROGRAM_ID> --treasury <TREASURY_ADDRESS>
```

Write the **recovery code** down on paper. Whoever has it controls the vault.

### 7. Deposit, check, withdraw

```bash
python3 qvault.py deposit 1                 # 1 SOL from your wallet into the vault
python3 qvault.py status
python3 qvault.py send <RECIPIENT> 0.25     # quantum-safe withdrawal
python3 qvault.py status                    # new vault #1 holds the rest
```

**With tokens** (free devnet USDC: https://faucet.circle.com → Solana Devnet):

```bash
USDC=4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU   # devnet USDC mint
python3 qvault.py deposit 10 --token $USDC
python3 qvault.py send <RECIPIENT> 2.5 --token $USDC
```

Every step is visible in the explorer: https://explorer.solana.com/?cluster=devnet

---

## What is tested

`cargo test` runs a real Solana bank with the SPL Token program and checks:

- **SOL flow** – open, deposit, commit, finish, fee exactly 0.1 %, remainder in
  the next vault; SOL arriving later still ends up in the next vault, no double payout
- **Signature attacks** – amount increased, recipient swapped, remainder
  redirected, different mint, single bit flipped → all rejected
- **Sweep / Finish attacks** – thief as recipient, next account or fee wallet →
  rejected; second commit with a spent key → rejected
- **Token flow** – two different tokens in one vault, payout + fee + forwarding,
  empty token accounts are closed
- **Cross-check** – the Python client and the Rust program produce bit-identical signatures

---

## Security notes and limitations

- **Prototype, not audited.** A professional security audit is required before
  any mainnet use.
- **Plain WOTS** (without the WOTS+ bitmasks). Safe for single use; WOTS+ or
  multi-target hardening is planned.
- **224-bit hash chains** give roughly 112-bit security against Grover's algorithm.
- The **recovery code** is stored in plain text in `~/.qvault/wallet.json`
  (mode 600). Production use needs keychain or hardware storage.
- **Token-2022 transfer hooks** are not supported yet.
- Each withdrawal leaves ~0.002 SOL rent in the old vault account, which serves
  as an immutable pointer to the next vault.

## Fees

Withdrawals to a third party carry a **0.1 %** fee. Rotating funds into your
own next vault is free.

## License

[MIT](LICENSE)

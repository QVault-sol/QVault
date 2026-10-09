//! End-to-end tests against a real bank (solana-program-test) including the SPL Token program.

use qvault::*;
use solana_instruction::{AccountMeta, Instruction};
use solana_keypair::Keypair;
use solana_program::{hash::hashv, pubkey::Pubkey, rent::Rent, sysvar};
use solana_program_test::*;
use solana_signer::Signer;
use solana_system_interface::{instruction as sys, program as system_program};
use solana_transaction::Transaction;

const SOL: u64 = 1_000_000_000;

// ───────────── WOTS signer (identical to the Python client) ─────────────
pub struct WotsKey {
    sk: Vec<[u8; N]>,
}

impl WotsKey {
    pub fn derive(seed: &[u8; 32], index: u32) -> Self {
        let sk = (0..L)
            .map(|i| {
                let h = hashv(&[b"QVAULT-SK", seed, &index.to_le_bytes(), &[i as u8]]).to_bytes();
                let mut x = [0u8; N];
                x.copy_from_slice(&h[..N]);
                x
            })
            .collect();
        Self { sk }
    }
    pub fn pk_hash(&self) -> [u8; 32] {
        let mut ends = Vec::with_capacity(SIG_LEN);
        for s in &self.sk {
            let mut x = *s;
            for _ in 0..W {
                x = chain_step(&x);
            }
            ends.extend_from_slice(&x);
        }
        hashv(&[&ends]).to_bytes()
    }
    pub fn sign(&self, digest: &[u8; N]) -> Vec<u8> {
        let c = chunks(digest);
        let mut sig = Vec::with_capacity(SIG_LEN);
        for (i, s) in self.sk.iter().enumerate() {
            let mut x = *s;
            for _ in 0..c[i] {
                x = chain_step(&x);
            }
            sig.extend_from_slice(&x);
        }
        sig
    }
    pub fn vault(&self, program_id: &Pubkey) -> (Pubkey, u8) {
        Pubkey::find_program_address(&[VAULT_SEED, &self.pk_hash()], program_id)
    }
}

// ───────────── Instruction builders ─────────────
fn ix_open(pid: &Pubkey, payer: &Pubkey, key: &WotsKey) -> Instruction {
    let (vault, bump) = key.vault(pid);
    let mut data = vec![0u8];
    data.extend_from_slice(&key.pk_hash());
    data.push(bump);
    Instruction {
        program_id: *pid,
        accounts: vec![
            AccountMeta::new(*payer, true),
            AccountMeta::new(vault, false),
            AccountMeta::new_readonly(system_program::ID, false),
            AccountMeta::new_readonly(sysvar::rent::ID, false),
        ],
        data,
    }
}

fn ix_commit(pid: &Pubkey, vault: Pubkey, recipient: Pubkey, next: Pubkey, mint: Pubkey, amount: u64, sig: &[u8]) -> Instruction {
    let mut data = vec![1u8];
    data.extend_from_slice(mint.as_ref());
    data.extend_from_slice(&amount.to_le_bytes());
    data.extend_from_slice(sig);
    Instruction {
        program_id: *pid,
        accounts: vec![
            AccountMeta::new(vault, false),
            AccountMeta::new_readonly(recipient, false),
            AccountMeta::new_readonly(next, false),
        ],
        data,
    }
}

#[allow(clippy::too_many_arguments)]
fn ix_sweep(pid: &Pubkey, vault: Pubkey, vault_tok: Pubkey, mint: Pubkey, next_tok: Pubkey, rcpt_tok: Pubkey, tres_tok: Pubkey, tp: Pubkey) -> Instruction {
    Instruction {
        program_id: *pid,
        accounts: vec![
            AccountMeta::new(vault, false),
            AccountMeta::new(vault_tok, false),
            AccountMeta::new_readonly(mint, false),
            AccountMeta::new(next_tok, false),
            AccountMeta::new(rcpt_tok, false),
            AccountMeta::new(tres_tok, false),
            AccountMeta::new_readonly(tp, false),
        ],
        data: vec![2u8],
    }
}

fn ix_finish(pid: &Pubkey, vault: Pubkey, next: Pubkey, recipient: Pubkey) -> Instruction {
    Instruction {
        program_id: *pid,
        accounts: vec![
            AccountMeta::new(vault, false),
            AccountMeta::new(next, false),
            AccountMeta::new(recipient, false),
            AccountMeta::new(TREASURY, false),
            AccountMeta::new_readonly(sysvar::rent::ID, false),
        ],
        data: vec![3u8],
    }
}

// ───────────── Test helpers ─────────────
async fn send(ctx: &mut ProgramTestContext, ixs: &[Instruction], extra: &[&Keypair]) -> Result<(), BanksClientError> {
    let bh = ctx.banks_client.get_latest_blockhash().await.unwrap();
    let mut signers: Vec<&Keypair> = vec![&ctx.payer];
    signers.extend_from_slice(extra);
    let tx = Transaction::new_signed_with_payer(ixs, Some(&ctx.payer.pubkey()), &signers, bh);
    ctx.banks_client.process_transaction(tx).await
}

async fn lamports(ctx: &mut ProgramTestContext, k: &Pubkey) -> u64 {
    ctx.banks_client.get_balance(*k).await.unwrap()
}

fn err_code(e: BanksClientError) -> Option<u32> {
    use solana_program::instruction::InstructionError;
    match e.unwrap() {
        solana_transaction::TransactionError::InstructionError(_, InstructionError::Custom(c)) => Some(c),
        _ => None,
    }
}

async fn start() -> (ProgramTestContext, Pubkey) {
    let pid = Pubkey::new_unique();
    let mut pt = ProgramTest::new("qvault", pid, processor!(process_instruction));
    pt.prefer_bpf(false);
    let mut ctx = pt.start_with_context().await;
    // Treasury exists (otherwise the first tiny fee would be below the rent minimum).
    let payer = ctx.payer.pubkey();
    send(&mut ctx, &[sys::transfer(&payer, &TREASURY, SOL)], &[]).await.unwrap();
    (ctx, pid)
}

async fn create_mint(ctx: &mut ProgramTestContext, decimals: u8, tp: Pubkey) -> Pubkey {
    let mint = Keypair::new();
    let rent = Rent::default().minimum_balance(82);
    let payer = ctx.payer.pubkey();
    let mut data = vec![20u8, decimals]; // InitializeMint2
    data.extend_from_slice(payer.as_ref());
    data.push(0); // no freeze authority
    data.extend_from_slice(&[0u8; 32]);
    let init = Instruction {
        program_id: tp,
        accounts: vec![AccountMeta::new(mint.pubkey(), false)],
        data,
    };
    send(ctx, &[sys::create_account(&payer, &mint.pubkey(), rent, 82, &tp), init], &[&mint])
        .await
        .unwrap();
    mint.pubkey()
}

async fn create_token_account(ctx: &mut ProgramTestContext, mint: &Pubkey, owner: &Pubkey, tp: Pubkey) -> Pubkey {
    let acc = Keypair::new();
    let rent = Rent::default().minimum_balance(165);
    let payer = ctx.payer.pubkey();
    let mut data = vec![18u8]; // InitializeAccount3
    data.extend_from_slice(owner.as_ref());
    let init = Instruction {
        program_id: tp,
        accounts: vec![AccountMeta::new(acc.pubkey(), false), AccountMeta::new_readonly(*mint, false)],
        data,
    };
    send(ctx, &[sys::create_account(&payer, &acc.pubkey(), rent, 165, &tp), init], &[&acc])
        .await
        .unwrap();
    acc.pubkey()
}

async fn mint_to(ctx: &mut ProgramTestContext, mint: &Pubkey, dest: &Pubkey, amount: u64, tp: Pubkey) {
    let mut data = vec![7u8]; // MintTo
    data.extend_from_slice(&amount.to_le_bytes());
    let payer = ctx.payer.pubkey();
    let ix = Instruction {
        program_id: tp,
        accounts: vec![
            AccountMeta::new(*mint, false),
            AccountMeta::new(*dest, false),
            AccountMeta::new_readonly(payer, true),
        ],
        data,
    };
    send(ctx, &[ix], &[]).await.unwrap();
}

async fn token_balance(ctx: &mut ProgramTestContext, acc: &Pubkey) -> Option<u64> {
    let a = ctx.banks_client.get_account(*acc).await.unwrap()?;
    Some(u64::from_le_bytes(a.data[64..72].try_into().unwrap()))
}

// ───────────── Tests ─────────────

#[tokio::test]
async fn sol_flow_and_attacks() {
    let (mut ctx, pid) = start().await;
    let payer = ctx.payer.pubkey();
    let keep = Rent::default().minimum_balance(STATE_LEN);
    let seed = [7u8; 32];
    let (k0, k1) = (WotsKey::derive(&seed, 0), WotsKey::derive(&seed, 1));
    let (v0, _) = k0.vault(&pid);
    let (v1, _) = k1.vault(&pid);
    let sol = Pubkey::default();

    send(&mut ctx, &[ix_open(&pid, &payer, &k0)], &[]).await.unwrap();
    send(&mut ctx, &[sys::transfer(&payer, &v0, 2 * SOL)], &[]).await.unwrap();
    send(&mut ctx, &[ix_open(&pid, &payer, &k0)], &[]).await.unwrap(); // idempotent
    send(&mut ctx, &[ix_open(&pid, &payer, &k1)], &[]).await.unwrap();
    assert_eq!(lamports(&mut ctx, &v0).await, keep + 2 * SOL);

    let recipient = Keypair::new().pubkey();
    let amount = SOL / 2;
    let sig = k0.sign(&message_digest(&pid, &v0, &recipient, &v1, &sol, amount));

    // Attacks on Commit
    let thief = Keypair::new().pubkey();
    let kt = WotsKey::derive(&[9u8; 32], 0);
    send(&mut ctx, &[ix_open(&pid, &payer, &kt)], &[]).await.unwrap();
    let (vt, _) = kt.vault(&pid);
    let mut flipped = sig.clone();
    flipped[333] ^= 0x80;
    for bad in [
        ix_commit(&pid, v0, recipient, v1, sol, amount + 1, &sig), // amount increased
        ix_commit(&pid, v0, thief, v1, sol, amount, &sig),         // recipient swapped
        ix_commit(&pid, v0, recipient, vt, sol, amount, &sig),     // remainder redirected
        ix_commit(&pid, v0, recipient, v1, Pubkey::new_unique(), amount, &sig), // different mint
        ix_commit(&pid, v0, recipient, v1, sol, amount, &flipped), // one bit flipped
    ] {
        let e = send(&mut ctx, &[bad], &[]).await.unwrap_err();
        assert_eq!(err_code(e), Some(QvError::BadSignature as u32));
    }

    // Recipients that could never receive the SOL are rejected before the key is spent
    let sys_prog = system_program::ID;
    let s_exec = k0.sign(&message_digest(&pid, &v0, &sys_prog, &v1, &sol, amount));
    let e = send(&mut ctx, &[ix_commit(&pid, v0, sys_prog, v1, sol, amount, &s_exec)], &[]).await.unwrap_err();
    assert_eq!(err_code(e), Some(QvError::BadRecipient as u32));
    let fresh = Keypair::new().pubkey();
    let s_dust = k0.sign(&message_digest(&pid, &v0, &fresh, &v1, &sol, 1_000));
    let e = send(&mut ctx, &[ix_commit(&pid, v0, fresh, v1, sol, 1_000, &s_dust)], &[]).await.unwrap_err();
    assert_eq!(err_code(e), Some(QvError::BadRecipient as u32));

    // Valid commit, then no second one possible
    send(&mut ctx, &[ix_commit(&pid, v0, recipient, v1, sol, amount, &sig)], &[]).await.unwrap();
    let e = send(&mut ctx, &[ix_commit(&pid, v0, recipient, v1, sol, amount, &sig)], &[]).await.unwrap_err();
    assert_eq!(err_code(e), Some(QvError::BadState as u32));

    // Finish with wrong destinations
    let e = send(&mut ctx, &[ix_finish(&pid, v0, v1, thief)], &[]).await.unwrap_err();
    assert_eq!(err_code(e), Some(QvError::WrongDestination as u32));
    let e = send(&mut ctx, &[ix_finish(&pid, v0, vt, recipient)], &[]).await.unwrap_err();
    assert_eq!(err_code(e), Some(QvError::WrongDestination as u32));

    // Finish – anyone may call it
    let tres_before = lamports(&mut ctx, &TREASURY).await;
    send(&mut ctx, &[ix_finish(&pid, v0, v1, recipient)], &[]).await.unwrap();
    let fee = fee_for(amount);
    assert_eq!(fee, 500_000); // 0.1 % of 0.5 SOL
    assert_eq!(lamports(&mut ctx, &recipient).await, amount);
    assert_eq!(lamports(&mut ctx, &TREASURY).await - tres_before, fee);
    assert_eq!(lamports(&mut ctx, &v0).await, keep);
    assert_eq!(lamports(&mut ctx, &v1).await, keep + 2 * SOL - amount - fee);

    // SOL arriving later still ends up in the next vault, no double payout
    send(&mut ctx, &[sys::transfer(&payer, &v0, SOL)], &[]).await.unwrap();
    send(&mut ctx, &[ix_finish(&pid, v0, v1, recipient)], &[]).await.unwrap();
    assert_eq!(lamports(&mut ctx, &recipient).await, amount);
    assert_eq!(lamports(&mut ctx, &v1).await, keep + 3 * SOL - amount - fee);

    // Amount too high -> InsufficientFunds at Finish
    let k2 = WotsKey::derive(&seed, 2);
    let (v2, _) = k2.vault(&pid);
    send(&mut ctx, &[ix_open(&pid, &payer, &k2)], &[]).await.unwrap();
    let big = 100 * SOL;
    let s1 = k1.sign(&message_digest(&pid, &v1, &recipient, &v2, &sol, big));
    send(&mut ctx, &[ix_commit(&pid, v1, recipient, v2, sol, big, &s1)], &[]).await.unwrap();
    let e = send(&mut ctx, &[ix_finish(&pid, v1, v2, recipient)], &[]).await.unwrap_err();
    assert_eq!(err_code(e), Some(QvError::InsufficientFunds as u32));
}

async fn token_flow_with(tp: Pubkey) {
    let (mut ctx, pid) = start().await;
    let payer = ctx.payer.pubkey();
    let keep = Rent::default().minimum_balance(STATE_LEN);
    let seed = [3u8; 32];
    let (k0, k1) = (WotsKey::derive(&seed, 0), WotsKey::derive(&seed, 1));
    let (v0, _) = k0.vault(&pid);
    let (v1, _) = k1.vault(&pid);
    send(&mut ctx, &[ix_open(&pid, &payer, &k0), ix_open(&pid, &payer, &k1)], &[]).await.unwrap();

    // Two tokens in the vault: "USDC" (6 decimals) and a second token
    let usdc = create_mint(&mut ctx, 6, tp).await;
    let other = create_mint(&mut ctx, 9, tp).await;
    let v0_usdc = create_token_account(&mut ctx, &usdc, &v0, tp).await;
    let v0_other = create_token_account(&mut ctx, &other, &v0, tp).await;
    mint_to(&mut ctx, &usdc, &v0_usdc, 1_000_000_000, tp).await; // 1000 USDC
    mint_to(&mut ctx, &other, &v0_other, 50, tp).await;

    let recipient = Keypair::new().pubkey();
    let r_usdc = create_token_account(&mut ctx, &usdc, &recipient, tp).await;
    let t_usdc = create_token_account(&mut ctx, &usdc, &TREASURY, tp).await;
    let v1_usdc = create_token_account(&mut ctx, &usdc, &v1, tp).await;
    let v1_other = create_token_account(&mut ctx, &other, &v1, tp).await;
    let thief = Keypair::new().pubkey();
    let thief_usdc = create_token_account(&mut ctx, &usdc, &thief, tp).await;

    // Sweep before Commit is rejected
    let e = send(&mut ctx, &[ix_sweep(&pid, v0, v0_usdc, usdc, v1_usdc, r_usdc, t_usdc, tp)], &[])
        .await
        .unwrap_err();
    assert_eq!(err_code(e), Some(QvError::BadState as u32));

    let amount = 100_000_000; // 100 USDC
    let sig = k0.sign(&message_digest(&pid, &v0, &recipient, &v1, &usdc, amount));
    send(&mut ctx, &[ix_commit(&pid, v0, recipient, v1, usdc, amount, &sig)], &[]).await.unwrap();

    // Attacks on Sweep: thief as recipient / next account / treasury
    for bad in [
        ix_sweep(&pid, v0, v0_usdc, usdc, v1_usdc, thief_usdc, t_usdc, tp),
        ix_sweep(&pid, v0, v0_usdc, usdc, thief_usdc, r_usdc, t_usdc, tp),
        ix_sweep(&pid, v0, v0_usdc, usdc, v1_usdc, r_usdc, thief_usdc, tp),
    ] {
        let e = send(&mut ctx, &[bad], &[]).await.unwrap_err();
        assert_eq!(err_code(e), Some(QvError::WrongDestination as u32));
    }

    // Other token: everything into the next vault
    send(&mut ctx, &[ix_sweep(&pid, v0, v0_other, other, v1_other, v1_other, v1_other, tp)], &[])
        .await
        .unwrap();
    assert_eq!(token_balance(&mut ctx, &v1_other).await, Some(50));
    assert_eq!(token_balance(&mut ctx, &v0_other).await, None); // closed

    // USDC: recipient + fee + remainder
    send(&mut ctx, &[ix_sweep(&pid, v0, v0_usdc, usdc, v1_usdc, r_usdc, t_usdc, tp)], &[])
        .await
        .unwrap();
    assert_eq!(token_balance(&mut ctx, &r_usdc).await, Some(amount));
    assert_eq!(token_balance(&mut ctx, &t_usdc).await, Some(100_000)); // 0.1 USDC
    assert_eq!(token_balance(&mut ctx, &v1_usdc).await, Some(1_000_000_000 - amount - 100_000));
    assert_eq!(token_balance(&mut ctx, &v0_usdc).await, None);

    // Finish: rent of the closed token accounts moves to the next vault
    let before = lamports(&mut ctx, &v1).await;
    send(&mut ctx, &[ix_finish(&pid, v0, v1, recipient)], &[]).await.unwrap();
    assert_eq!(lamports(&mut ctx, &v0).await, keep);
    assert_eq!(lamports(&mut ctx, &v1).await - before, 2 * Rent::default().minimum_balance(165));
    assert_eq!(lamports(&mut ctx, &recipient).await, 0); // token withdrawal: no SOL
}

#[tokio::test]
async fn token_flow() {
    token_flow_with(TOKEN_PROGRAM).await;
}

#[tokio::test]
async fn token_2022_flow() {
    token_flow_with(TOKEN_2022_PROGRAM).await;
}

/// Cross-check against a test vector generated by the Python client.
#[test]
fn python_vector() {
    let txt = std::fs::read_to_string(concat!(env!("CARGO_MANIFEST_DIR"), "/tests/vector.txt"))
        .expect("tests/vector.txt missing – generate it with `python3 client/qvault_core.py`");
    let m: std::collections::HashMap<_, _> = txt
        .lines()
        .filter_map(|l| l.split_once('='))
        .map(|(k, v)| (k.trim().to_string(), v.trim().to_string()))
        .collect();
    let hex = |s: &str| -> Vec<u8> {
        (0..s.len()).step_by(2).map(|i| u8::from_str_radix(&s[i..i + 2], 16).unwrap()).collect()
    };
    let pk = |s: &str| Pubkey::new_from_array(hex(s).try_into().unwrap());

    let digest = message_digest(
        &pk(&m["program_id"]),
        &pk(&m["vault"]),
        &pk(&m["recipient"]),
        &pk(&m["next_vault"]),
        &pk(&m["mint"]),
        m["amount"].parse().unwrap(),
    );
    assert_eq!(digest.to_vec(), hex(&m["digest"]));
    let key = WotsKey::derive(&hex(&m["seed"]).try_into().unwrap(), m["index"].parse().unwrap());
    assert_eq!(key.pk_hash().to_vec(), hex(&m["pk_hash"]));
    assert_eq!(key.sign(&digest), hex(&m["sig"]));
    assert_eq!(recover_pk_hash(&hex(&m["sig"]), &digest).to_vec(), hex(&m["pk_hash"]));
}

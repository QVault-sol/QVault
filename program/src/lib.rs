//! QVault – a quantum-safe vault for SOL and SPL tokens on Solana.
//!
//! Security model
//! --------------
//! Every vault is a PDA that only this program can move. Withdrawals are
//! authorised with a Winternitz one-time signature (WOTS, w = 256, 224-bit
//! chains), verified with SHA-256 only – no elliptic curves anywhere.
//!
//! Because each key may sign exactly ONCE, a withdrawal works like this:
//!   1. `Commit`  – one-time signature over (recipient, mint, amount, next vault).
//!                  The vault is now "rotating": all destinations are fixed.
//!   2. `SweepToken` (per token) and `Finish` (SOL) – NO signature, callable by
//!      anyone. They pay the recipient + fee and move EVERYTHING else into the
//!      next vault. Any number of tokens, across any number of transactions –
//!      nobody can change the destinations any more.
//!
//! Instructions (first byte = tag)
//!   0 Open       { pk_hash: [u8;32], bump: u8 }
//!                [payer (s,w), vault (w), system_program, rent_sysvar]
//!   1 Commit     { mint: [u8;32] (0…0 = SOL), amount: u64, sig: [u8;840] }
//!                [vault (w), recipient, next_vault]
//!   2 SweepToken {}
//!                [vault (w), vault_token (w), mint, next_token (w),
//!                 recipient_token (w), treasury_token (w), token_program]
//!   3 Finish     {}
//!                [vault (w), next_vault (w), recipient (w), treasury (w), rent_sysvar]

use solana_program::{
    account_info::{next_account_info, AccountInfo},
    entrypoint::ProgramResult,
    hash::hashv,
    instruction::{AccountMeta, Instruction},
    msg,
    program::{invoke, invoke_signed},
    program_error::ProgramError,
    pubkey,
    pubkey::Pubkey,
    rent::Rent,
    sysvar::SysvarSerialize,
};
use solana_system_interface::instruction as system_instruction;

#[cfg(not(feature = "no-entrypoint"))]
solana_program::entrypoint!(process_instruction);

// ───────────────────────── Parameters ─────────────────────────

/// Fee recipient. REPLACE WITH YOUR OWN WALLET BEFORE DEPLOYING.
pub const TREASURY: Pubkey = pubkey!("BvohK534Gc8DpuarvMMRvGjJ7amSgwMPwxr85tr3jXX2");
/// Fee in basis points on withdrawals to third parties (10 = 0.1 %).
/// Rotation into the next vault is free.
pub const FEE_BPS: u64 = 10;

pub const N: usize = 28;
pub const MSG_CHUNKS: usize = N;
pub const CS_CHUNKS: usize = 2;
pub const L: usize = MSG_CHUNKS + CS_CHUNKS;
pub const SIG_LEN: usize = L * N; // 840
pub const W: u16 = 255;

pub const VAULT_SEED: &[u8] = b"vault";
pub const DOMAIN: &[u8] = b"QVAULT-v2";

pub const TOKEN_PROGRAM: Pubkey = pubkey!("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA");
pub const TOKEN_2022_PROGRAM: Pubkey = pubkey!("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb");

// ───────────────────────── State ─────────────────────────

pub const STATUS_ACTIVE: u8 = 1;
pub const STATUS_ROTATING: u8 = 2;

/// Vault data layout (139 bytes):
///   0 status | 1 bump | 2..34 pk_hash | 34..66 next_vault | 66..98 recipient
///   98..130 mint (0 = SOL) | 130..138 amount | 138 paid
pub const STATE_LEN: usize = 139;

pub struct State {
    pub status: u8,
    pub bump: u8,
    pub pk_hash: [u8; 32],
    pub next_vault: Pubkey,
    pub recipient: Pubkey,
    pub mint: Pubkey,
    pub amount: u64,
    pub paid: bool,
}

impl State {
    pub fn load(d: &[u8]) -> Result<Self, ProgramError> {
        if d.len() != STATE_LEN {
            return Err(QvError::BadState.into());
        }
        let pk = |a: usize| Pubkey::new_from_array(d[a..a + 32].try_into().unwrap());
        Ok(Self {
            status: d[0],
            bump: d[1],
            pk_hash: d[2..34].try_into().unwrap(),
            next_vault: pk(34),
            recipient: pk(66),
            mint: pk(98),
            amount: u64::from_le_bytes(d[130..138].try_into().unwrap()),
            paid: d[138] != 0,
        })
    }
    pub fn store(&self, d: &mut [u8]) {
        d[0] = self.status;
        d[1] = self.bump;
        d[2..34].copy_from_slice(&self.pk_hash);
        d[34..66].copy_from_slice(self.next_vault.as_ref());
        d[66..98].copy_from_slice(self.recipient.as_ref());
        d[98..130].copy_from_slice(self.mint.as_ref());
        d[130..138].copy_from_slice(&self.amount.to_le_bytes());
        d[138] = self.paid as u8;
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum QvError {
    BadInstruction = 0,
    WrongOwner = 1,
    BadSignature = 2,
    InsufficientFunds = 3,
    SameAccount = 4,
    BadState = 5,
    WrongDestination = 6,
    BadTokenAccount = 7,
    BadRecipient = 8,
}

impl From<QvError> for ProgramError {
    fn from(e: QvError) -> Self {
        ProgramError::Custom(e as u32)
    }
}

// ───────────────────────── WOTS ─────────────────────────

/// Chain function F(x) = SHA-256(x)[..28]
#[inline(always)]
pub fn chain_step(x: &[u8; N]) -> [u8; N] {
    let h = hashv(&[x]).to_bytes();
    let mut out = [0u8; N];
    out.copy_from_slice(&h[..N]);
    out
}

/// Signed digest – binds program, vault, recipient, next vault, mint and amount.
pub fn message_digest(
    program_id: &Pubkey,
    vault: &Pubkey,
    recipient: &Pubkey,
    next_vault: &Pubkey,
    mint: &Pubkey,
    amount: u64,
) -> [u8; N] {
    let h = hashv(&[
        DOMAIN,
        program_id.as_ref(),
        vault.as_ref(),
        recipient.as_ref(),
        next_vault.as_ref(),
        mint.as_ref(),
        &amount.to_le_bytes(),
    ])
    .to_bytes();
    let mut out = [0u8; N];
    out.copy_from_slice(&h[..N]);
    out
}

/// 28 message chunks + 2 checksum chunks (big endian).
pub fn chunks(digest: &[u8; N]) -> [u8; L] {
    let mut c = [0u8; L];
    let mut checksum: u16 = 0;
    for i in 0..MSG_CHUNKS {
        c[i] = digest[i];
        checksum += W - digest[i] as u16;
    }
    c[MSG_CHUNKS] = (checksum >> 8) as u8;
    c[MSG_CHUNKS + 1] = (checksum & 0xff) as u8;
    c
}

/// Recompute the public-key hash from a signature and digest.
pub fn recover_pk_hash(sig: &[u8], digest: &[u8; N]) -> [u8; 32] {
    let c = chunks(digest);
    let mut ends = [0u8; SIG_LEN];
    for i in 0..L {
        let mut x = [0u8; N];
        x.copy_from_slice(&sig[i * N..(i + 1) * N]);
        for _ in (c[i] as u16)..W {
            x = chain_step(&x);
        }
        ends[i * N..(i + 1) * N].copy_from_slice(&x);
    }
    hashv(&[&ends]).to_bytes()
}

pub fn fee_for(amount: u64) -> u64 {
    ((amount as u128 * FEE_BPS as u128) / 10_000) as u64
}

// ───────────────────────── Entrypoint ─────────────────────────

pub fn process_instruction(
    program_id: &Pubkey,
    accounts: &[AccountInfo],
    data: &[u8],
) -> ProgramResult {
    let (tag, rest) = data.split_first().ok_or(QvError::BadInstruction)?;
    match tag {
        0 => open(program_id, accounts, rest),
        1 => commit(program_id, accounts, rest),
        2 => sweep_token(program_id, accounts, rest),
        3 => finish(program_id, accounts, rest),
        _ => Err(QvError::BadInstruction.into()),
    }
}

fn load_vault(program_id: &Pubkey, vault: &AccountInfo) -> Result<State, ProgramError> {
    if vault.owner != program_id {
        return Err(QvError::WrongOwner.into());
    }
    State::load(&vault.try_borrow_data()?)
}

// ───────────────────────── 0: Open ─────────────────────────

fn open(program_id: &Pubkey, accounts: &[AccountInfo], data: &[u8]) -> ProgramResult {
    if data.len() != 33 {
        return Err(QvError::BadInstruction.into());
    }
    let pk_hash: [u8; 32] = data[..32].try_into().unwrap();
    let bump = data[32];

    let it = &mut accounts.iter();
    let payer = next_account_info(it)?;
    let vault = next_account_info(it)?;
    let system_program = next_account_info(it)?;
    let rent_info = next_account_info(it)?;

    if !payer.is_signer {
        return Err(ProgramError::MissingRequiredSignature);
    }
    let expected = Pubkey::create_program_address(&[VAULT_SEED, &pk_hash, &[bump]], program_id)?;
    if expected != *vault.key {
        return Err(ProgramError::InvalidSeeds);
    }
    if vault.owner == program_id {
        msg!("Vault already open");
        return Ok(());
    }

    let seeds: &[&[u8]] = &[VAULT_SEED, &pk_hash, &[bump]];
    let rent_min = Rent::from_account_info(rent_info)?.minimum_balance(STATE_LEN);

    if vault.lamports() == 0 {
        invoke_signed(
            &system_instruction::create_account(
                payer.key,
                vault.key,
                rent_min,
                STATE_LEN as u64,
                program_id,
            ),
            &[payer.clone(), vault.clone(), system_program.clone()],
            &[seeds],
        )?;
    } else {
        // Address was pre-funded with SOL: top up, allocate, take ownership.
        let missing = rent_min.saturating_sub(vault.lamports());
        if missing > 0 {
            invoke(
                &system_instruction::transfer(payer.key, vault.key, missing),
                &[payer.clone(), vault.clone(), system_program.clone()],
            )?;
        }
        invoke_signed(
            &system_instruction::allocate(vault.key, STATE_LEN as u64),
            &[vault.clone(), system_program.clone()],
            &[seeds],
        )?;
        invoke_signed(
            &system_instruction::assign(vault.key, program_id),
            &[vault.clone(), system_program.clone()],
            &[seeds],
        )?;
    }

    State {
        status: STATUS_ACTIVE,
        bump,
        pk_hash,
        next_vault: Pubkey::default(),
        recipient: Pubkey::default(),
        mint: Pubkey::default(),
        amount: 0,
        paid: false,
    }
    .store(&mut vault.try_borrow_mut_data()?);
    msg!("Vault opened");
    Ok(())
}

// ───────────────────────── 1: Commit ─────────────────────────

fn commit(program_id: &Pubkey, accounts: &[AccountInfo], data: &[u8]) -> ProgramResult {
    if data.len() != 32 + 8 + SIG_LEN {
        return Err(QvError::BadInstruction.into());
    }
    let mint = Pubkey::new_from_array(data[..32].try_into().unwrap());
    let amount = u64::from_le_bytes(data[32..40].try_into().unwrap());
    let sig = &data[40..];

    let it = &mut accounts.iter();
    let vault = next_account_info(it)?;
    let recipient = next_account_info(it)?;
    let next_vault = next_account_info(it)?;

    let mut st = load_vault(program_id, vault)?;
    if st.status != STATUS_ACTIVE {
        return Err(QvError::BadState.into());
    }
    if load_vault(program_id, next_vault)?.status != STATUS_ACTIVE {
        return Err(QvError::BadState.into());
    }
    if vault.key == next_vault.key || vault.key == recipient.key {
        return Err(QvError::SameAccount.into());
    }
    // A SOL payout must be able to land, otherwise the rotating vault could never finish:
    // executable accounts cannot be credited, and a new account needs the rent-exempt minimum.
    if mint == Pubkey::default()
        && amount > 0
        && (recipient.executable
            || (recipient.lamports() == 0 && amount < Rent::default().minimum_balance(0)))
    {
        msg!("Recipient cannot receive this SOL payout");
        return Err(QvError::BadRecipient.into());
    }

    let digest = message_digest(program_id, vault.key, recipient.key, next_vault.key, &mint, amount);
    if recover_pk_hash(sig, &digest) != st.pk_hash {
        msg!("Invalid signature");
        return Err(QvError::BadSignature.into());
    }

    st.status = STATUS_ROTATING;
    st.next_vault = *next_vault.key;
    st.recipient = *recipient.key;
    st.mint = mint;
    st.amount = amount;
    st.paid = amount == 0;
    st.store(&mut vault.try_borrow_mut_data()?);
    msg!("Withdrawal committed – vault rotating");
    Ok(())
}

// ───────────────────────── 2: SweepToken ─────────────────────────

/// (mint, owner, amount) of a token account (Token & Token-2022, base layout).
fn read_token_account(acc: &AccountInfo) -> Result<(Pubkey, Pubkey, u64), ProgramError> {
    if *acc.owner != TOKEN_PROGRAM && *acc.owner != TOKEN_2022_PROGRAM {
        return Err(QvError::BadTokenAccount.into());
    }
    let d = acc.try_borrow_data()?;
    if d.len() < 165 || d[108] != 1 {
        return Err(QvError::BadTokenAccount.into());
    }
    Ok((
        Pubkey::new_from_array(d[0..32].try_into().unwrap()),
        Pubkey::new_from_array(d[32..64].try_into().unwrap()),
        u64::from_le_bytes(d[64..72].try_into().unwrap()),
    ))
}

fn read_decimals(mint: &AccountInfo) -> Result<u8, ProgramError> {
    if *mint.owner != TOKEN_PROGRAM && *mint.owner != TOKEN_2022_PROGRAM {
        return Err(QvError::BadTokenAccount.into());
    }
    let d = mint.try_borrow_data()?;
    if d.len() < 82 {
        return Err(QvError::BadTokenAccount.into());
    }
    Ok(d[44])
}

#[allow(clippy::too_many_arguments)]
fn token_transfer_checked<'a>(
    token_program: &AccountInfo<'a>,
    source: &AccountInfo<'a>,
    mint: &AccountInfo<'a>,
    dest: &AccountInfo<'a>,
    authority: &AccountInfo<'a>,
    amount: u64,
    decimals: u8,
    seeds: &[&[u8]],
) -> ProgramResult {
    if amount == 0 {
        return Ok(());
    }
    let mut data = Vec::with_capacity(10);
    data.push(12u8); // TransferChecked
    data.extend_from_slice(&amount.to_le_bytes());
    data.push(decimals);
    let ix = Instruction {
        program_id: *token_program.key,
        accounts: vec![
            AccountMeta::new(*source.key, false),
            AccountMeta::new_readonly(*mint.key, false),
            AccountMeta::new(*dest.key, false),
            AccountMeta::new_readonly(*authority.key, true),
        ],
        data,
    };
    invoke_signed(
        &ix,
        &[source.clone(), mint.clone(), dest.clone(), authority.clone(), token_program.clone()],
        &[seeds],
    )
}

fn sweep_token(program_id: &Pubkey, accounts: &[AccountInfo], data: &[u8]) -> ProgramResult {
    if !data.is_empty() {
        return Err(QvError::BadInstruction.into());
    }
    let it = &mut accounts.iter();
    let vault = next_account_info(it)?;
    let vault_token = next_account_info(it)?;
    let mint = next_account_info(it)?;
    let next_token = next_account_info(it)?;
    let recipient_token = next_account_info(it)?;
    let treasury_token = next_account_info(it)?;
    let token_program = next_account_info(it)?;

    if *token_program.key != TOKEN_PROGRAM && *token_program.key != TOKEN_2022_PROGRAM {
        return Err(QvError::BadTokenAccount.into());
    }
    let mut st = load_vault(program_id, vault)?;
    if st.status != STATUS_ROTATING {
        return Err(QvError::BadState.into());
    }

    let (src_mint, src_owner, balance) = read_token_account(vault_token)?;
    if src_mint != *mint.key || src_owner != *vault.key {
        return Err(QvError::BadTokenAccount.into());
    }
    let (n_mint, n_owner, _) = read_token_account(next_token)?;
    if n_mint != *mint.key || n_owner != st.next_vault {
        return Err(QvError::WrongDestination.into());
    }
    let decimals = read_decimals(mint)?;
    let bump = [st.bump];
    let pk_hash = st.pk_hash;
    let seeds: &[&[u8]] = &[VAULT_SEED, &pk_hash, &bump];

    let mut rest = balance;
    if st.mint == *mint.key && !st.paid {
        let (r_mint, r_owner, _) = read_token_account(recipient_token)?;
        if r_mint != *mint.key || r_owner != st.recipient {
            return Err(QvError::WrongDestination.into());
        }
        let (t_mint, t_owner, _) = read_token_account(treasury_token)?;
        if t_mint != *mint.key || t_owner != TREASURY {
            return Err(QvError::WrongDestination.into());
        }
        let fee = fee_for(st.amount);
        let need = st.amount.checked_add(fee).ok_or(QvError::InsufficientFunds)?;
        if balance < need {
            return Err(QvError::InsufficientFunds.into());
        }
        token_transfer_checked(token_program, vault_token, mint, recipient_token, vault, st.amount, decimals, seeds)?;
        token_transfer_checked(token_program, vault_token, mint, treasury_token, vault, fee, decimals, seeds)?;
        rest = balance - need;
        st.paid = true;
        st.store(&mut vault.try_borrow_mut_data()?);
    }
    token_transfer_checked(token_program, vault_token, mint, next_token, vault, rest, decimals, seeds)?;

    // Close the empty token account – rent goes back to the vault (Finish moves it on).
    let close = Instruction {
        program_id: *token_program.key,
        accounts: vec![
            AccountMeta::new(*vault_token.key, false),
            AccountMeta::new(*vault.key, false),
            AccountMeta::new_readonly(*vault.key, true),
        ],
        data: vec![9u8], // CloseAccount
    };
    invoke_signed(&close, &[vault_token.clone(), vault.clone(), token_program.clone()], &[seeds])?;
    msg!("Token swept");
    Ok(())
}

// ───────────────────────── 3: Finish ─────────────────────────

fn finish(program_id: &Pubkey, accounts: &[AccountInfo], data: &[u8]) -> ProgramResult {
    if !data.is_empty() {
        return Err(QvError::BadInstruction.into());
    }
    let it = &mut accounts.iter();
    let vault = next_account_info(it)?;
    let next_vault = next_account_info(it)?;
    let recipient = next_account_info(it)?;
    let treasury = next_account_info(it)?;
    let rent_info = next_account_info(it)?;

    let mut st = load_vault(program_id, vault)?;
    if st.status != STATUS_ROTATING {
        return Err(QvError::BadState.into());
    }
    if *next_vault.key != st.next_vault || *recipient.key != st.recipient || *treasury.key != TREASURY {
        return Err(QvError::WrongDestination.into());
    }

    // The vault account stays (pointer to the next vault); only the excess moves.
    let keep = Rent::from_account_info(rent_info)?.minimum_balance(STATE_LEN);
    let mut excess = vault.lamports().saturating_sub(keep);

    if st.mint == Pubkey::default() && !st.paid {
        let fee = fee_for(st.amount);
        let need = st.amount.checked_add(fee).ok_or(QvError::InsufficientFunds)?;
        if excess < need {
            return Err(QvError::InsufficientFunds.into());
        }
        **vault.try_borrow_mut_lamports()? -= need;
        **recipient.try_borrow_mut_lamports()? += st.amount;
        **treasury.try_borrow_mut_lamports()? += fee;
        excess -= need;
        st.paid = true;
        st.store(&mut vault.try_borrow_mut_data()?);
    }
    **vault.try_borrow_mut_lamports()? -= excess;
    **next_vault.try_borrow_mut_lamports()? += excess;
    msg!("SOL swept: {} lamports to next vault", excess);
    Ok(())
}

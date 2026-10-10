// QVault web app (devnet). A normal wallet pays network fees; funds live in a vault
// that only opens with a Winternitz one-time signature computed in this browser.
import {
  ComputeBudgetProgram,
  Connection,
  Keypair,
  LAMPORTS_PER_SOL,
  PublicKey,
  SystemProgram,
  Transaction,
} from "@solana/web3.js";
import * as qc from "./core.js";

const CONFIG = {
  rpc: "https://api.devnet.solana.com",
  programId: new PublicKey("DwBtsKCpRjWyo3HmQ9U9twDLF3xya4Cs2Eoq7fQFLLLo"),
  treasury: new PublicKey("GcDnFhLESFBYmyGfq5L9bTgf1aAuiV7tZdk3cH2G5LPD"),
  explorer: (kind, id) => `https://explorer.solana.com/${kind}/${id}?cluster=devnet`,
};
const DEVNET_GENESIS = "EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG";
const MAINNET_GENESIS = "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d";
const DEVNET_USDC = "4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU";
const SOL_RESERVE = 10_000_000n; // keep 0.01 SOL in the wallet for fees

// Public devnet RPC rate-limits bursts (HTTP 429): back off and retry instead of failing.
async function patientFetch(url, init) {
  let res;
  for (let i = 0; i < 7; i++) {
    res = await fetch(url, init);
    if (res.status !== 429) return res;
    await sleep(600 * 2 ** i + Math.random() * 400);
  }
  return res;
}
const conn = new Connection(CONFIG.rpc, { commitment: "confirmed", fetch: patientFetch, disableRetryOnRateLimit: true });
const PID = CONFIG.programId;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ───────────── storage (per browser) ─────────────
const store = {
  get(k) { try { return JSON.parse(localStorage.getItem("qvault." + k)); } catch { return null; } },
  set(k, v) { try { localStorage.setItem("qvault." + k, JSON.stringify(v)); } catch { /* private mode */ } },
};

// ───────────── state ─────────────
const S = {
  wallet: null, // { publicKey, signTransaction, kind }
  seed: null,
  index: 0,
  vaultState: null,
  vaultTokens: [],
  walletTokens: [],
  walletLamports: 0n,
  rentKeep: 0n,
  busy: false,
};

const $ = (id) => document.getElementById(id);
const short = (pk) => { const s = pk.toString(); return s.slice(0, 4) + "…" + s.slice(-4); };

function fmtUnits(units, decimals, maxFrac = decimals) {
  const neg = units < 0n;
  const s = (neg ? -units : units).toString().padStart(decimals + 1, "0");
  const int = s.slice(0, s.length - decimals);
  let frac = decimals ? s.slice(-decimals).slice(0, maxFrac).replace(/0+$/, "") : "";
  return (neg ? "-" : "") + BigInt(int).toLocaleString("en-US") + (frac ? "." + frac : "");
}

function parseUnits(str, decimals) {
  const t = String(str).trim().replace(",", ".");
  if (!/^\d+(\.\d+)?$/.test(t)) throw new Error("Enter a positive number, for example 0.25");
  const [i, f = ""] = t.split(".");
  if (f.length > decimals) throw new Error(`At most ${decimals} decimal places`);
  const v = BigInt(i) * 10n ** BigInt(decimals) + BigInt((f + "0".repeat(decimals)).slice(0, decimals) || "0");
  if (v <= 0n) throw new Error("Amount must be greater than zero");
  return v;
}

const ERRORS = {
  0: "Malformed instruction", 1: "Account is not a QVault vault", 2: "Signature does not match this vault",
  3: "Not enough funds in the vault (amount + 0.1 % fee)", 4: "Recipient cannot be the vault itself",
  5: "Vault is in the wrong state for this step", 6: "Destination does not match the signed withdrawal",
  7: "Unsupported token account", 8: "That recipient cannot receive this SOL payout",
};
function explainError(e) {
  const m = String(e?.message || e);
  const custom = m.match(/"Custom":\s*(\d+)/) || m.match(/custom program error: 0x([0-9a-f]+)/i);
  if (custom) {
    const code = custom[0].includes("0x") ? parseInt(custom[1], 16) : Number(custom[1]);
    if (ERRORS[code]) return ERRORS[code];
  }
  if (/User rejected/i.test(m)) return "You rejected the request in your wallet.";
  if (/insufficient lamports|Attempt to debit an account but found no record/i.test(m))
    return "Your wallet has no devnet SOL for fees. Use “Get devnet SOL” first.";
  return m.length > 240 ? m.slice(0, 240) + "…" : m;
}

// ───────────── feedback: toasts, activity, phases, checklist ─────────────
function toast(text, kind = "ok", href) {
  const el = document.createElement("div");
  el.className = "toast" + (kind === "err" ? " err" : "");
  el.textContent = text + " ";
  if (href) {
    const a = document.createElement("a");
    a.href = href; a.target = "_blank"; a.rel = "noopener"; a.textContent = "View";
    el.append(a);
  }
  $("toasts").append(el);
  setTimeout(() => el.remove(), kind === "err" ? 9000 : 5000);
}

function logStep(text) {
  const li = document.createElement("li");
  li.className = "step run";
  li.innerHTML = `<span class="dot" aria-hidden="true"></span><span class="txt"></span><span class="lnk"></span>`;
  li.querySelector(".txt").textContent = text;
  $("log").prepend(li);
  $("log-empty").hidden = true;
  return {
    done(sig) {
      li.className = "step ok";
      if (sig) {
        const a = document.createElement("a");
        a.href = CONFIG.explorer("tx", sig); a.target = "_blank"; a.rel = "noopener"; a.textContent = "Explorer";
        li.querySelector(".lnk").append(a);
      }
    },
    fail(msg) {
      li.className = "step err";
      li.querySelector(".txt").textContent = text + " – " + msg;
    },
  };
}

const PHASES = ["sign", "commit", "payout", "rotate"];
function phase(name, state, sig) {
  const li = document.querySelector(`.phases li[data-p="${name}"]`);
  if (!li) return;
  li.className = state;
  li.querySelector("a")?.remove();
  if (sig) {
    const a = document.createElement("a");
    a.href = CONFIG.explorer("tx", sig); a.target = "_blank"; a.rel = "noopener"; a.textContent = "View on explorer";
    li.append(a);
  }
}
const resetPhases = () => PHASES.forEach((p) => phase(p, ""));

function demoStep(k, state) {
  const li = document.querySelector(`#demo-steps li[data-k="${k}"]`);
  if (li) li.className = state;
}

// ───────────── chain helpers ─────────────
async function sendTx(ixs, label, extraSigners = []) {
  const step = logStep(label);
  try {
    const tx = new Transaction().add(...ixs);
    tx.feePayer = S.wallet.publicKey;
    const { blockhash, lastValidBlockHeight } = await conn.getLatestBlockhash("confirmed");
    tx.recentBlockhash = blockhash;
    if (extraSigners.length) tx.partialSign(...extraSigners);
    const signed = await S.wallet.signTransaction(tx);
    const sig = await conn.sendRawTransaction(signed.serialize(), { preflightCommitment: "confirmed" });
    await waitFor(sig, lastValidBlockHeight);
    step.done(sig);
    return sig;
  } catch (e) {
    step.fail(explainError(e));
    throw e;
  }
}

// Poll instead of websockets: works behind proxies and on flaky mobile networks.
async function waitFor(sig, lastValidBlockHeight) {
  for (let i = 0; i < 90; i++) {
    const { value } = await conn.getSignatureStatuses([sig]);
    const st = value[0];
    if (st?.err) throw new Error(JSON.stringify(st.err));
    if (st && (st.confirmationStatus === "confirmed" || st.confirmationStatus === "finalized")) return;
    if (i % 5 === 4 && lastValidBlockHeight && (await conn.getBlockHeight("confirmed")) > lastValidBlockHeight)
      throw new Error("Transaction expired before it landed. Please try again.");
    await sleep(1500);
  }
  throw new Error("Not confirmed after 2 minutes. Check the activity link later.");
}

async function vaultState(vault) {
  const acc = await conn.getAccountInfo(vault, "confirmed");
  if (!acc || !acc.owner.equals(PID)) return null;
  return { ...qc.decodeState(new Uint8Array(acc.data)), lamports: BigInt(acc.lamports) };
}

async function tokenAccounts(owner) {
  const out = [];
  for (const programId of [qc.TOKEN_PROGRAM, qc.TOKEN_2022_PROGRAM]) {
    const res = await conn.getParsedTokenAccountsByOwner(owner, { programId }, "confirmed");
    for (const it of res.value) {
      const info = it.account.data.parsed.info;
      out.push({
        address: it.pubkey,
        mint: new PublicKey(info.mint),
        amount: BigInt(info.tokenAmount.amount),
        decimals: info.tokenAmount.decimals,
        program: programId,
      });
    }
  }
  return out;
}

const key = (i = S.index) => new qc.WotsKey(S.seed, i);
const currentVault = () => key().vault(PID)[0];

async function ensureOpen(k, label) {
  const [vault] = k.vault(PID);
  if (!(await vaultState(vault))) await sendTx([qc.ixOpen(PID, S.wallet.publicKey, k)], label);
  return vault;
}

function tokenLabel(mint) {
  const tm = store.get("testMint");
  if (tm && tm.mint === mint.toString()) return "qUSD";
  if (mint.toString() === DEVNET_USDC) return "USDC";
  return short(mint);
}

// ───────────── wallet ─────────────
function phantomProvider() {
  const p = window.phantom?.solana ?? window.solana;
  return p?.isPhantom ? p : null;
}

async function connectPhantom() {
  const p = phantomProvider();
  if (!p) {
    window.open("https://phantom.com/download", "_blank", "noopener");
    return;
  }
  const res = await p.connect();
  S.wallet = { kind: "Phantom", publicKey: new PublicKey(res.publicKey.toString()), signTransaction: (tx) => p.signTransaction(tx) };
  await afterConnect();
}

async function useDemoWallet() {
  let raw = store.get("burner");
  if (!raw) {
    raw = Array.from(Keypair.generate().secretKey);
    store.set("burner", raw);
  }
  const kp = Keypair.fromSecretKey(Uint8Array.from(raw));
  S.wallet = { kind: "Demo wallet", publicKey: kp.publicKey, signTransaction: async (tx) => { tx.partialSign(kp); return tx; } };
  await afterConnect();
}

function requireFunds(minSol = 0.003) {
  if (S.walletLamports < BigInt(Math.round(minSol * LAMPORTS_PER_SOL)))
    throw new Error(`Your wallet needs at least ${minSol} devnet SOL for this. Get free devnet SOL at faucet.solana.com ` +
      "(choose Devnet). Using Phantom? Turn on Settings → Developer Settings → Testnet Mode and pick Solana Devnet.");
}

async function afterConnect() {
  const genesis = await conn.getGenesisHash();
  if (genesis === MAINNET_GENESIS) throw new Error("This demo refuses to run on mainnet. Real funds must not be used.");
  if (genesis !== DEVNET_GENESIS) throw new Error("The RPC endpoint is not Solana devnet. Stopping.");
  $("landing").hidden = true;
  $("app").hidden = false;
  $("wallet-chip").hidden = false;
  $("disconnect").hidden = false;
  $("chip-addr").textContent = short(S.wallet.publicKey);
  $("wallet-kind").textContent = S.wallet.kind;
  $("wallet-addr").textContent = S.wallet.publicKey.toString();
  S.rentKeep = BigInt(await conn.getMinimumBalanceForRentExemption(qc.STATE_LEN));
  const saved = store.get("seed");
  if (saved) {
    S.seed = qc.fromHex(saved);
    S.index = store.get("index") ?? 0;
  }
  await refresh();
  selectTab(S.seed && S.vaultState && (S.vaultState.lamports - S.rentKeep > 0n || S.vaultTokens.length) ? "send" : "deposit");
}

function disconnect() {
  S.wallet = null;
  $("app").hidden = true;
  $("landing").hidden = false;
  $("wallet-chip").hidden = true;
  $("disconnect").hidden = true;
}

async function airdrop() {
  const step = logStep("Requesting 1 devnet SOL");
  try {
    const sig = await conn.requestAirdrop(S.wallet.publicKey, LAMPORTS_PER_SOL);
    await waitFor(sig);
    step.done(sig);
    toast("1 devnet SOL received");
  } catch {
    step.fail("the public faucet is busy. Use faucet.solana.com with your wallet address.");
    throw new Error("The devnet airdrop is rate-limited right now. Copy your wallet address and use faucet.solana.com (Devnet).");
  }
}

// ───────────── vault lifecycle ─────────────
function newSeed() {
  S.seed = crypto.getRandomValues(new Uint8Array(32));
  S.index = 0;
  store.set("seed", qc.toHex(S.seed));
  store.set("index", 0);
  store.set("pending", null);
}

async function createVault() {
  requireFunds();
  newSeed();
  await ensureOpen(key(), "Opening your quantum-safe vault");
  $("recovery").open = true;
  toast("Vault created. Write down your recovery code.");
  selectTab("deposit");
}

async function restoreVault(code) {
  const seed = qc.fromHex(code);
  S.seed = seed;
  S.index = 0;
  const step = logStep("Looking for your current vault");
  for (;;) {
    const st = await vaultState(key().vault(PID)[0]);
    if (!st || st.status === 1) break;
    if (st.status === 2 && st.lamports - S.rentKeep > 0n) break; // unfinished withdrawal
    S.index++;
  }
  store.set("seed", qc.toHex(S.seed));
  store.set("index", S.index);
  step.done();
  await ensureOpen(key(), "Opening vault #" + S.index);
  toast(`Restored. Current vault is #${S.index}.`);
}

// Token-2022 extensions that change transfer behaviour would leave tokens stuck in a rotating vault.
const SAFE_2022_EXTENSIONS = new Set(["metadataPointer", "tokenMetadata", "groupPointer", "groupMemberPointer",
  "tokenGroup", "tokenGroupMember", "mintCloseAuthority", "immutableOwner"]);

async function assertSupportedMint(mint, program) {
  if (!program.equals(qc.TOKEN_2022_PROGRAM)) return;
  const info = await conn.getParsedAccountInfo(mint, "confirmed");
  const exts = info.value?.data?.parsed?.info?.extensions ?? [];
  const bad = exts.map((e) => e.extension).filter((e) => !SAFE_2022_EXTENSIONS.has(e));
  if (bad.length) throw new Error(`This Token-2022 mint uses ${bad.join(", ")}, which QVault does not support yet. Deposit refused so the tokens cannot get stuck.`);
}

async function depositCore(assetSel, amountStr) {
  const vault = currentVault();
  if (assetSel === "SOL") {
    const lamports = parseUnits(amountStr, 9);
    if (lamports + 5000n > S.walletLamports) throw new Error("Your wallet does not hold that much SOL");
    const sig = await sendTx([SystemProgram.transfer({ fromPubkey: S.wallet.publicKey, toPubkey: vault, lamports })],
      `Depositing ${amountStr} SOL`);
    toast(`Deposited ${amountStr} SOL`, "ok", CONFIG.explorer("tx", sig));
  } else {
    const t = S.walletTokens.find((x) => x.address.toString() === assetSel);
    if (!t) throw new Error("Pick a token from your wallet");
    const units = parseUnits(amountStr, t.decimals);
    if (units > t.amount) throw new Error("Your wallet does not hold that many tokens");
    await assertSupportedMint(t.mint, t.program);
    const sig = await sendTx([
      qc.ixCreateAtaIdempotent(S.wallet.publicKey, vault, t.mint, t.program),
      qc.ixTransferChecked(t.address, t.mint, qc.ata(vault, t.mint, t.program), S.wallet.publicKey, units, t.decimals, t.program),
    ], `Depositing ${amountStr} ${tokenLabel(t.mint)}`);
    toast(`Deposited ${amountStr} ${tokenLabel(t.mint)}`, "ok", CONFIG.explorer("tx", sig));
  }
}

async function withdrawCore({ recipientStr, assetSel, amountStr, resumeOnly = false }) {
  const idx = S.index;
  const k = key(idx);
  const nextK = key(idx + 1);
  const [vault] = k.vault(PID);
  const [nextVault] = nextK.vault(PID);
  let st = await vaultState(vault);
  if (!st) throw new Error("Open a vault first");
  resetPhases();
  $("flow").scrollIntoView?.({ behavior: "smooth", block: "nearest" });
  let label = "";

  if (st.status === 2) {
    phase("sign", "ok");
    phase("commit", "ok");
    logStep("Signed withdrawal found on-chain, finishing it").done();
  } else {
    if (resumeOnly) return;
    let recipient;
    try { recipient = new PublicKey(String(recipientStr).trim()); } catch { throw new Error("Recipient is not a valid Solana address"); }
    let mint = qc.SOL_MINT, decimals = 9;
    if (assetSel !== "SOL") {
      const t = S.vaultTokens.find((x) => x.mint.toString() === assetSel);
      if (!t) throw new Error("That token is not in the vault");
      mint = t.mint;
      decimals = t.decimals;
    }
    const amount = parseUnits(amountStr, decimals);
    const fee = qc.feeFor(amount);
    label = `${fmtUnits(amount, decimals)} ${mint.equals(qc.SOL_MINT) ? "SOL" : tokenLabel(mint)}`;
    const request = { index: idx, recipient: recipient.toString(), mint: mint.toString(), amount: amount.toString() };
    const pend = store.get("pending");
    if (pend && JSON.stringify(pend) !== JSON.stringify(request))
      throw new Error("A signed withdrawal is waiting to land. Repeat it with exactly the same values: "
        + `${pend.amount} units to ${pend.recipient}. A one-time key must never sign twice.`);

    if (mint.equals(qc.SOL_MINT)) {
      const excess = st.lamports - S.rentKeep;
      if (excess < amount + fee) throw new Error(`Not enough SOL: the vault holds ${fmtUnits(excess, 9)}, you need ${fmtUnits(amount + fee, 9)} including the fee`);
      const rcpt = await conn.getAccountInfo(recipient);
      if (rcpt?.executable) throw new Error("That address is a program and cannot receive SOL");
      if (!rcpt && amount < BigInt(await conn.getMinimumBalanceForRentExemption(0)))
        throw new Error("New recipient accounts need at least 0.00089 SOL");
    } else {
      const bal = S.vaultTokens.filter((x) => x.mint.equals(mint)).reduce((s, x) => s + x.amount, 0n);
      if (bal < amount + fee) throw new Error(`Not enough tokens: the vault holds ${fmtUnits(bal, decimals)}, you need ${fmtUnits(amount + fee, decimals)} including the fee`);
    }

    await ensureOpen(nextK, `Preparing next vault #${idx + 1}`);
    phase("sign", "run");
    const s1 = logStep("Computing an 840-byte Winternitz signature in this browser");
    await sleep(30); // let the UI paint before the hashing loop
    const sig = k.sign(qc.messageDigest(PID, vault, recipient, nextVault, mint, amount));
    s1.done();
    phase("sign", "ok");
    phase("commit", "run");
    store.set("pending", request); // from here on this key counts as used
    try {
      const csig = await sendTx([ComputeBudgetProgram.setComputeUnitLimit({ units: 1_400_000 }),
        qc.ixCommit(PID, vault, recipient, nextVault, mint, amount, sig)],
        "Commit: program verifies the hash signature on-chain");
      phase("commit", "ok", csig);
    } catch (e) {
      phase("commit", "err");
      // Rejected in simulation means never broadcast, so the signature stayed private.
      if (/Simulation failed|simulation failed/.test(String(e?.message))) store.set("pending", null);
      throw e;
    }
    st = await vaultState(vault);
  }

  const payer = S.wallet.publicKey;
  phase("payout", "run");
  let payoutSig = null;
  try {
    for (const t of await tokenAccounts(vault)) {
      const nextTok = qc.ata(st.nextVault, t.mint, t.program);
      const ixs = [qc.ixCreateAtaIdempotent(payer, st.nextVault, t.mint, t.program)];
      let rTok = nextTok, trTok = nextTok;
      const isPayout = t.mint.equals(st.mint) && !st.paid;
      if (isPayout) {
        rTok = qc.ata(st.recipient, t.mint, t.program);
        trTok = qc.ata(CONFIG.treasury, t.mint, t.program);
        ixs.push(qc.ixCreateAtaIdempotent(payer, st.recipient, t.mint, t.program),
          qc.ixCreateAtaIdempotent(payer, CONFIG.treasury, t.mint, t.program));
      }
      ixs.push(qc.ixSweep(PID, vault, t.address, t.mint, nextTok, rTok, trTok, t.program));
      const s = await sendTx(ixs, isPayout ? `Paying out ${tokenLabel(t.mint)}, moving the rest on`
        : `Moving ${tokenLabel(t.mint)} to the next vault`);
      if (isPayout) payoutSig = s;
    }
    const fsig = await sendTx([qc.ixFinish(PID, vault, st.nextVault, st.recipient, CONFIG.treasury)],
      st.mint.equals(qc.SOL_MINT) ? "Paying out SOL, moving the rest to the next vault" : "Moving remaining SOL to the next vault");
    phase("payout", "ok", payoutSig ?? fsig);
    phase("rotate", "ok", fsig);
  } catch (e) {
    phase("payout", "err");
    throw e;
  }

  S.index = idx + 1;
  store.set("index", S.index);
  store.set("pending", null);
  spinDoor();
  logStep(`Vault #${idx} is spent. Your funds are in vault #${S.index} under a fresh key.`).done();
  toast(label ? `Sent ${label} quantum-safe` : "Withdrawal finished");
}

async function mintCore() {
  const me = S.wallet.publicKey;
  const stored = store.get("testMint");
  let mintPk = stored && stored.owner === me.toString() ? new PublicKey(stored.mint) : null;
  const ixs = [];
  let signers = [];
  if (!mintPk || !(await conn.getAccountInfo(mintPk))) {
    const mintKp = Keypair.generate();
    mintPk = mintKp.publicKey;
    signers = [mintKp];
    ixs.push(
      SystemProgram.createAccount({ fromPubkey: me, newAccountPubkey: mintPk,
        lamports: await conn.getMinimumBalanceForRentExemption(82), space: 82, programId: qc.TOKEN_PROGRAM }),
      qc.ixInitMint2(mintPk, me, 6),
    );
  }
  ixs.push(qc.ixCreateAtaIdempotent(me, me, mintPk), qc.ixMintTo(mintPk, qc.ata(me, mintPk), me, 1000n * 10n ** 6n));
  await sendTx(ixs, "Minting 1,000 qUSD test tokens to your wallet", signers);
  store.set("testMint", { owner: me.toString(), mint: mintPk.toString() });
  toast("1,000 qUSD test tokens are in your wallet");
  return mintPk;
}

// Public RPC lists new token accounts with a short delay.
async function waitForToken(owner, mint, minAmount = 1n) {
  for (let i = 0; i < 30; i++) {
    const t = (await tokenAccounts(owner)).find((x) => x.mint.equals(mint) && x.amount >= minAmount);
    if (t) return t;
    await sleep(2000);
  }
  throw new Error("The token account did not show up on the devnet RPC yet. Press the button again.");
}

// One click for judges: SOL and an SPL token go in, and both come back out with hash signatures.
async function guidedDemo() {
  requireFunds(0.08);
  const me = S.wallet.publicKey;
  document.querySelectorAll("#demo-steps li").forEach((li) => (li.className = ""));
  const step = async (k, fn) => {
    demoStep(k, "run");
    try { const r = await fn(); demoStep(k, "ok"); await refresh(); return r; } catch (e) { demoStep(k, "err"); throw e; }
  };
  await step("open", async () => {
    if (!S.seed) newSeed();
    await ensureOpen(key(), "Opening your quantum-safe vault");
  });
  await step("sol-in", () => depositCore("SOL", "0.05"));
  const mint = await step("mint", () => mintCore());
  await step("tok-in", async () => {
    const walletTok = await waitForToken(me, mint);
    S.walletTokens = await tokenAccounts(me);
    await depositCore(walletTok.address.toString(), "10");
  });
  await step("sol-out", () => withdrawCore({ recipientStr: me.toString(), assetSel: "SOL", amountStr: "0.01" }));
  await step("tok-out", async () => {
    await waitForToken(currentVault(), mint);
    await refresh();
    await withdrawCore({ recipientStr: me.toString(), assetSel: mint.toString(), amountStr: "2" });
  });
  logStep("Guided demo complete: 0.01 SOL and 2 qUSD withdrawn to your wallet, the rest rotated to a fresh vault.").done();
  toast("Guided demo complete");
}

// ───────────── UI ─────────────
async function run(btn, fn) {
  if (S.busy) return;
  S.busy = true;
  document.body.classList.add("busy");
  btn?.setAttribute("aria-busy", "true");
  $("error").hidden = true;
  try {
    await fn();
  } catch (e) {
    const msg = explainError(e);
    $("error").textContent = msg;
    $("error").hidden = false;
    toast(msg, "err");
  } finally {
    S.busy = false;
    document.body.classList.remove("busy");
    btn?.removeAttribute("aria-busy");
    await refresh().catch(() => {});
  }
}

function drawDoor(svg, pkHash) {
  const cx = 120, cy = 120, rf = 112, rd = rf * 0.74;
  const NS = "http://www.w3.org/2000/svg";
  const el = (n, a) => { const e = document.createElementNS(NS, n); for (const k in a) e.setAttribute(k, a[k]); return e; };
  svg.replaceChildren();
  const g = el("g", { class: "door" });
  g.append(el("circle", { cx, cy, r: rf, class: "frame" }), el("circle", { cx, cy, r: rf * 0.93, class: "ring" }),
    el("circle", { cx, cy, r: rd, class: "leaf" }));
  for (let i = 0; i < 30; i++) {
    const a = -Math.PI / 2 + (i * 2 * Math.PI) / 30;
    const L = pkHash ? 0.15 + 0.85 * (pkHash[i] / 255) : 0.55 + 0.35 * Math.sin(i * 1.7) ** 2;
    const r0 = rd * 0.9, r1 = rd + (rf * 0.93 - rd) * L;
    const x0 = cx + r0 * Math.cos(a), y0 = cy + r0 * Math.sin(a);
    g.append(el("rect", { x: x0, y: y0 - 2.6, width: r1 - r0, height: 5.2, rx: 1.3, class: "bolt",
      transform: `rotate(${(a * 180) / Math.PI} ${x0} ${y0})` }));
  }
  for (let k = 0; k < 3; k++) {
    const a = -Math.PI / 2 + (k * 2 * Math.PI) / 3 + Math.PI / 6;
    const x1 = cx + rd * 0.55 * Math.cos(a), y1 = cy + rd * 0.55 * Math.sin(a);
    g.append(el("line", { x1: cx, y1: cy, x2: x1, y2: y1, class: "spoke" }), el("circle", { cx: x1, cy: y1, r: 5, class: "knob" }));
  }
  g.append(el("circle", { cx, cy, r: 19, class: "hub" }), el("circle", { cx, cy, r: 7, class: "knob" }));
  svg.append(g);
}

let doorKey = null;
function spinDoor() {
  const g = document.querySelector("#door .door");
  if (!g) return;
  g.classList.remove("spin");
  void g.getBoundingClientRect();
  g.classList.add("spin");
}

function chips(containerId, name, items, emptyText) {
  const box = $(containerId);
  const prev = box.querySelector("input:checked")?.value;
  box.replaceChildren();
  if (!items.length) {
    const p = document.createElement("p");
    p.className = "none";
    p.textContent = emptyText;
    box.append(p);
    return;
  }
  items.forEach((it, i) => {
    const lab = document.createElement("label");
    lab.className = "chip";
    lab.innerHTML = `<input type="radio" name="${name}"><span><b></b><small></small></span>`;
    const input = lab.querySelector("input");
    input.value = it.value;
    input.checked = prev ? prev === it.value : i === 0;
    lab.querySelector("b").textContent = it.label;
    lab.querySelector("small").textContent = it.sub;
    box.append(lab);
  });
  if (!box.querySelector("input:checked")) box.querySelector("input").checked = true;
}
const chosen = (name) => document.querySelector(`input[name="${name}"]:checked`)?.value;

function selectTab(name) {
  for (const t of ["send", "deposit", "demo"]) {
    $("tab-" + t).setAttribute("aria-selected", String(t === name));
    $("panel-" + t).hidden = t !== name;
  }
}

async function refresh() {
  if (!S.wallet) return;
  const solBal = BigInt(await conn.getBalance(S.wallet.publicKey, "confirmed"));
  S.walletLamports = solBal;
  $("wallet-sol").textContent = fmtUnits(solBal, 9, 4) + " SOL";
  $("chip-sol").textContent = fmtUnits(solBal, 9, 3) + " SOL";
  $("no-sol").hidden = solBal >= 10_000_000n;
  S.walletTokens = await tokenAccounts(S.wallet.publicKey);
  chips("dep-assets", "dep-asset", [
    { value: "SOL", label: "SOL", sub: fmtUnits(solBal, 9, 4) },
    ...S.walletTokens.filter((t) => t.amount > 0n).map((t) =>
      ({ value: t.address.toString(), label: tokenLabel(t.mint), sub: fmtUnits(t.amount, t.decimals, 2) })),
  ], "");

  const hasVault = !!S.seed;
  $("no-vault").hidden = hasVault;
  $("vault").hidden = !hasVault;
  if (!hasVault) {
    $("vault-index").textContent = "";
    $("vault-badge").hidden = true;
    chips("send-assets", "send-asset", [], "Create a vault and deposit first.");
    return;
  }

  const k = key();
  const [vault] = k.vault(PID);
  const st = await vaultState(vault);
  S.vaultState = st;
  if (doorKey !== `${S.index}`) { drawDoor($("door"), k.pkHash()); doorKey = `${S.index}`; }
  $("vault-index").textContent = "#" + S.index;
  $("sum-key").textContent = String(S.index);
  $("vault-addr").textContent = vault.toString();
  $("vault-link").href = CONFIG.explorer("address", vault.toString());
  $("recovery-code").textContent = qc.toHex(S.seed);
  const rows = [];
  const sendItems = [];
  if (st) {
    const sol = st.lamports - S.rentKeep > 0n ? st.lamports - S.rentKeep : 0n;
    rows.push(["SOL", fmtUnits(sol, 9, 4)]);
    if (sol > 0n) sendItems.push({ value: "SOL", label: "SOL", sub: fmtUnits(sol, 9, 4) });
    S.vaultTokens = await tokenAccounts(vault);
    for (const t of S.vaultTokens) {
      rows.push([tokenLabel(t.mint), fmtUnits(t.amount, t.decimals, 2)]);
      if (t.amount > 0n) sendItems.push({ value: t.mint.toString(), label: tokenLabel(t.mint), sub: fmtUnits(t.amount, t.decimals, 2) });
    }
  } else {
    S.vaultTokens = [];
  }
  chips("send-assets", "send-asset", sendItems, "The vault is empty. Deposit something first.");
  $("balances").replaceChildren(...rows.map(([a, v]) => {
    const tr = document.createElement("tr");
    tr.innerHTML = "<th scope=row></th><td></td>";
    tr.children[0].textContent = a;
    tr.children[1].textContent = v;
    return tr;
  }));
  const rotating = st && st.status === 2;
  $("vault-badge").hidden = !st;
  $("vault-badge").textContent = rotating ? "Withdrawal in progress" : "Locked";
  $("vault-badge").className = "badge" + (rotating ? " busy" : "");
  $("resume").hidden = !rotating;
  updateSummary();
}

function sendAsset() {
  const sel = chosen("send-asset");
  const t = S.vaultTokens.find((x) => x.mint.toString() === sel);
  return { sel, t, decimals: t ? t.decimals : 9, label: t ? tokenLabel(t.mint) : "SOL" };
}

function updateSummary() {
  const { decimals, label } = sendAsset();
  try {
    const amt = parseUnits($("send-amount").value, decimals);
    const fee = qc.feeFor(amt);
    $("sum-fee").textContent = `${fmtUnits(fee, decimals)} ${label}`;
    $("sum-total").textContent = `${fmtUnits(amt + fee, decimals)} ${label}`;
  } catch {
    $("sum-fee").textContent = "0.1 % of the amount";
    $("sum-total").textContent = "–";
  }
}

function maxSend() {
  const { sel, t, decimals } = sendAsset();
  let bal = 0n;
  if (sel === "SOL" && S.vaultState) bal = S.vaultState.lamports - S.rentKeep;
  else if (t) bal = t.amount;
  const max = bal > 0n ? (bal * 10000n) / 10010n : 0n; // leave room for the 0.1 % fee
  $("send-amount").value = max > 0n ? fmtUnits(max, decimals).replace(/,/g, "") : "";
  updateSummary();
}

function maxDeposit() {
  const sel = chosen("dep-asset");
  if (sel === "SOL") {
    const v = S.walletLamports > SOL_RESERVE ? S.walletLamports - SOL_RESERVE : 0n;
    $("dep-amount").value = v > 0n ? fmtUnits(v, 9).replace(/,/g, "") : "";
  } else {
    const t = S.walletTokens.find((x) => x.address.toString() === sel);
    if (t) $("dep-amount").value = fmtUnits(t.amount, t.decimals).replace(/,/g, "");
  }
}

function downloadBackup() {
  const blob = new Blob([`QVault recovery code (devnet)\n${qc.toHex(S.seed)}\nProgram: ${PID}\n`], { type: "text/plain" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "qvault-recovery-code.txt";
  a.click();
  URL.revokeObjectURL(a.href);
}

function copy(text, what) {
  navigator.clipboard?.writeText(text).then(() => toast(`${what} copied`), () => {});
}

function bind() {
  drawDoor($("door-hero"), null);
  drawDoor($("door-empty"), null);
  $("door-empty").classList.add("empty-door");
  $("phantom-hint").hidden = !!phantomProvider();

  $("phantom-btn").onclick = (e) => run(e.currentTarget, connectPhantom);
  $("demo-btn").onclick = (e) => run(e.currentTarget, useDemoWallet);
  $("disconnect").onclick = disconnect;
  $("airdrop-btn").onclick = (e) => run(e.currentTarget, airdrop);
  $("mint-btn").onclick = (e) => run(e.currentTarget, async () => { requireFunds(); await mintCore(); });
  $("create-btn").onclick = (e) => run(e.currentTarget, createVault);
  $("restore-form").onsubmit = (e) => {
    e.preventDefault();
    run(e.submitter, () => restoreVault($("restore-code").value));
  };
  $("dep-form").onsubmit = (e) => {
    e.preventDefault();
    const sel = chosen("dep-asset"), amt = $("dep-amount").value;
    run(e.submitter, async () => {
      requireFunds();
      if (!S.seed) throw new Error("Create a vault first");
      await depositCore(sel, amt);
      $("dep-amount").value = "";
    });
  };
  $("send-form").onsubmit = (e) => {
    e.preventDefault();
    const args = { recipientStr: $("send-to").value, assetSel: chosen("send-asset"), amountStr: $("send-amount").value };
    run(e.submitter, async () => {
      requireFunds();
      if (!args.assetSel) throw new Error("The vault is empty. Deposit something first.");
      await withdrawCore(args);
      $("send-amount").value = "";
    });
  };
  $("resume").onclick = (e) => run(e.currentTarget, () => withdrawCore({ resumeOnly: true }));
  $("demo-run").onclick = (e) => run(e.currentTarget, guidedDemo);
  $("to-self").onclick = () => { $("send-to").value = S.wallet.publicKey.toString(); };
  $("send-max").onclick = maxSend;
  $("dep-max").onclick = maxDeposit;
  $("send-amount").oninput = updateSummary;
  $("send-assets").onchange = () => { $("send-amount").value = ""; updateSummary(); };
  $("copy-vault").onclick = () => copy($("vault-addr").textContent, "Vault address");
  $("copy-wallet").onclick = () => copy(S.wallet.publicKey.toString(), "Wallet address");
  $("copy-code").onclick = () => copy(qc.toHex(S.seed), "Recovery code");
  $("download-code").onclick = downloadBackup;
  for (const t of ["send", "deposit", "demo"]) $("tab-" + t).onclick = () => selectTab(t);

  // Public RPC nodes index new token accounts with a short delay: refresh while idle.
  setInterval(() => { if (S.wallet && !S.busy) refresh().catch(() => {}); }, 15000);
}

bind();

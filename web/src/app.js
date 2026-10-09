// QVault web app (devnet). Wallet pays network fees; funds live in a vault that
// only opens with a Winternitz one-time signature computed in this browser.
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
const conn = new Connection(CONFIG.rpc, "confirmed");
const PID = CONFIG.programId;

// ───────────── storage (per browser) ─────────────
const store = {
  get(k) {
    try { return JSON.parse(localStorage.getItem("qvault." + k)); } catch { return null; }
  },
  set(k, v) {
    try { localStorage.setItem("qvault." + k, JSON.stringify(v)); } catch { /* private mode */ }
  },
};

// ───────────── state ─────────────
const S = {
  wallet: null, // { publicKey, signTransaction, kind }
  seed: null, // Uint8Array(32)
  index: 0,
  vaultState: null,
  vaultTokens: [],
  walletTokens: [],
  rentKeep: null,
  busy: false,
};

const $ = (id) => document.getElementById(id);
const short = (pk) => { const s = pk.toString(); return s.slice(0, 4) + "…" + s.slice(-4); };

function fmtUnits(units, decimals) {
  const neg = units < 0n;
  let s = (neg ? -units : units).toString().padStart(decimals + 1, "0");
  const int = s.slice(0, s.length - decimals);
  let frac = decimals ? s.slice(-decimals).replace(/0+$/, "") : "";
  return (neg ? "-" : "") + Number(int).toLocaleString("en-US") + (frac ? "." + frac : "");
}

function parseUnits(str, decimals) {
  const t = String(str).trim();
  if (!/^\d+(\.\d+)?$/.test(t)) throw new Error("Enter a positive number, e.g. 0.25");
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
  7: "Unsupported token account",
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
  return m.length > 220 ? m.slice(0, 220) + "…" : m;
}

// ───────────── activity log ─────────────
function logStep(text, state = "run") {
  const li = document.createElement("li");
  li.className = "step " + state;
  li.innerHTML = `<span class="dot" aria-hidden="true"></span><span class="txt"></span>`;
  li.querySelector(".txt").textContent = text;
  $("log").prepend(li);
  $("log-empty").hidden = true;
  return {
    done(sig, label = "View transaction") {
      li.className = "step ok";
      if (sig) {
        const a = document.createElement("a");
        a.href = CONFIG.explorer("tx", sig);
        a.target = "_blank";
        a.rel = "noopener";
        a.textContent = label;
        li.append(a);
      }
    },
    fail(msg) {
      li.className = "step err";
      li.querySelector(".txt").textContent = text + " — " + msg;
    },
  };
}

// ───────────── transactions ─────────────
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
    await new Promise((r) => setTimeout(r, 1000));
  }
  throw new Error("Not confirmed after 90 s – check the activity link later");
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

async function mintInfo(mint) {
  const acc = await conn.getAccountInfo(mint, "confirmed");
  if (!acc || !(acc.owner.equals(qc.TOKEN_PROGRAM) || acc.owner.equals(qc.TOKEN_2022_PROGRAM)))
    throw new Error("Not a token mint");
  return { decimals: acc.data[44], program: acc.owner };
}

const key = (i = S.index) => new qc.WotsKey(S.seed, i);
const currentVault = () => key().vault(PID)[0];

async function ensureOpen(k, label) {
  const [vault] = k.vault(PID);
  if (!(await vaultState(vault))) await sendTx([qc.ixOpen(PID, S.wallet.publicKey, k)], label);
  return vault;
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

async function afterConnect() {
  $("wallet-btn").textContent = short(S.wallet.publicKey);
  $("wallet-kind").textContent = S.wallet.kind;
  $("wallet-addr").textContent = S.wallet.publicKey.toString();
  $("connect").hidden = true;
  $("app").hidden = false;
  $("exposed-key").textContent = short(S.wallet.publicKey);
  S.rentKeep = BigInt(await conn.getMinimumBalanceForRentExemption(qc.STATE_LEN));
  const saved = store.get("seed");
  if (saved) {
    S.seed = qc.fromHex(saved);
    S.index = store.get("index") ?? 0;
  }
  await refresh();
}

async function airdrop() {
  const step = logStep("Requesting 1 devnet SOL");
  try {
    const sig = await conn.requestAirdrop(S.wallet.publicKey, LAMPORTS_PER_SOL);
    await waitFor(sig);
    step.done(sig);
  } catch {
    step.fail("the public faucet is rate-limited right now. Use faucet.solana.com with the address above.");
  }
  await refresh();
}

// ───────────── vault lifecycle ─────────────
async function createVault() {
  S.seed = crypto.getRandomValues(new Uint8Array(32));
  S.index = 0;
  store.set("seed", qc.toHex(S.seed));
  store.set("index", 0);
  store.set("pending", null);
  await run(async () => {
    await ensureOpen(key(), "Opening your quantum-safe vault");
    showRecovery(true);
  });
}

async function restoreVault() {
  const input = prompt("Paste your 64-character recovery code");
  if (!input) return;
  let seed;
  try { seed = qc.fromHex(input); } catch (e) { alert(e.message); return; }
  S.seed = seed;
  S.index = 0;
  await run(async () => {
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
  });
}

async function deposit() {
  const assetSel = $("dep-asset").value;
  const amountStr = $("dep-amount").value;
  await run(async () => {
    const vault = currentVault();
    if (assetSel === "SOL") {
      const lamports = parseUnits(amountStr, 9);
      await sendTx([SystemProgram.transfer({ fromPubkey: S.wallet.publicKey, toPubkey: vault, lamports })],
        `Depositing ${amountStr} SOL`);
    } else {
      const t = S.walletTokens.find((x) => x.address.toString() === assetSel);
      const units = parseUnits(amountStr, t.decimals);
      if (units > t.amount) throw new Error("Your wallet does not hold that many tokens");
      await sendTx([
        qc.ixCreateAtaIdempotent(S.wallet.publicKey, vault, t.mint, t.program),
        qc.ixTransferChecked(t.address, t.mint, qc.ata(vault, t.mint, t.program), S.wallet.publicKey, units, t.decimals, t.program),
      ], `Depositing ${amountStr} ${tokenLabel(t.mint)}`);
    }
    $("dep-amount").value = "";
  });
}

async function withdraw(resumeOnly = false) {
  await run(async () => {
    const idx = S.index;
    const k = key(idx);
    const nextK = key(idx + 1);
    const [vault] = k.vault(PID);
    const [nextVault] = nextK.vault(PID);
    let st = await vaultState(vault);
    if (!st) throw new Error("Vault not opened yet");

    if (st.status === 2) {
      logStep("Signed withdrawal found on-chain – finishing it").done();
    } else {
      if (resumeOnly) return;
      const recipient = new PublicKey($("send-to").value.trim());
      const assetSel = $("send-asset").value;
      let mint = qc.SOL_MINT, decimals = 9;
      if (assetSel !== "SOL") {
        const t = S.vaultTokens.find((x) => x.mint.toString() === assetSel);
        mint = t.mint;
        decimals = t.decimals;
      }
      const amount = parseUnits($("send-amount").value, decimals);
      const fee = qc.feeFor(amount);
      const request = { index: idx, recipient: recipient.toString(), mint: mint.toString(), amount: amount.toString() };
      const pend = store.get("pending");
      if (pend && JSON.stringify(pend) !== JSON.stringify(request))
        throw new Error("A signed withdrawal is waiting to land. Repeat it with exactly the same values: "
          + `${pend.amount} units to ${pend.recipient}. A one-time key must never sign twice.`);

      if (mint.equals(qc.SOL_MINT)) {
        const excess = st.lamports - S.rentKeep;
        if (excess < amount + fee) throw new Error(`Not enough SOL: the vault holds ${fmtUnits(excess, 9)}, you need ${fmtUnits(amount + fee, 9)} incl. fee`);
        const rcpt = await conn.getAccountInfo(recipient);
        if (!rcpt && amount < BigInt(await conn.getMinimumBalanceForRentExemption(0)))
          throw new Error("New recipient accounts need at least 0.00089 SOL");
      } else {
        const bal = S.vaultTokens.filter((x) => x.mint.equals(mint)).reduce((s, x) => s + x.amount, 0n);
        if (bal < amount + fee) throw new Error(`Not enough tokens: the vault holds ${fmtUnits(bal, decimals)}, you need ${fmtUnits(amount + fee, decimals)} incl. fee`);
      }

      await ensureOpen(nextK, `Preparing next vault #${idx + 1}`);
      const s1 = logStep("Computing Winternitz one-time signature in your browser");
      const sig = k.sign(qc.messageDigest(PID, vault, recipient, nextVault, mint, amount));
      s1.done();
      store.set("pending", request); // from here on this key counts as used
      await sendTx([ComputeBudgetProgram.setComputeUnitLimit({ units: 1_400_000 }),
        qc.ixCommit(PID, vault, recipient, nextVault, mint, amount, sig)],
        "Quantum-safe commit: verifying 840-byte hash signature on-chain");
      st = await vaultState(vault);
    }

    const payer = S.wallet.publicKey;
    for (const t of await tokenAccounts(vault)) {
      const nextTok = qc.ata(st.nextVault, t.mint, t.program);
      const ixs = [qc.ixCreateAtaIdempotent(payer, st.nextVault, t.mint, t.program)];
      let rTok = nextTok, trTok = nextTok;
      if (t.mint.equals(st.mint) && !st.paid) {
        rTok = qc.ata(st.recipient, t.mint, t.program);
        trTok = qc.ata(CONFIG.treasury, t.mint, t.program);
        ixs.push(qc.ixCreateAtaIdempotent(payer, st.recipient, t.mint, t.program),
          qc.ixCreateAtaIdempotent(payer, CONFIG.treasury, t.mint, t.program));
      }
      ixs.push(qc.ixSweep(PID, vault, t.address, t.mint, nextTok, rTok, trTok, t.program));
      await sendTx(ixs, `Paying out and moving ${tokenLabel(t.mint)} to the new vault`);
    }
    await sendTx([qc.ixFinish(PID, vault, st.nextVault, st.recipient, CONFIG.treasury)],
      "Moving remaining SOL to the new vault");

    S.index = idx + 1;
    store.set("index", S.index);
    store.set("pending", null);
    $("send-amount").value = "";
    document.querySelector(".door")?.classList.add("rotate");
    logStep(`Done. Vault #${idx} is spent; your funds are in vault #${S.index} with a fresh key.`).done();
  });
}

async function mintTestTokens() {
  await run(async () => {
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
  });
}

// ───────────── UI ─────────────
function tokenLabel(mint) {
  const tm = store.get("testMint");
  if (tm && tm.mint === mint.toString()) return "qUSD";
  if (mint.toString() === "4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU") return "USDC";
  return short(mint);
}

async function run(fn) {
  if (S.busy) return;
  S.busy = true;
  document.body.classList.add("busy");
  $("error").hidden = true;
  try {
    await fn();
  } catch (e) {
    $("error").textContent = explainError(e);
    $("error").hidden = false;
  } finally {
    S.busy = false;
    document.body.classList.remove("busy");
    await refresh().catch(() => {});
  }
}

function drawDoor(pkHash) {
  const svg = $("door");
  const cx = 120, cy = 120, rf = 112, rd = rf * 0.74;
  const NS = "http://www.w3.org/2000/svg";
  const el = (n, a) => { const e = document.createElementNS(NS, n); for (const k in a) e.setAttribute(k, a[k]); return e; };
  svg.replaceChildren();
  const g = el("g", { class: "door" });
  g.append(el("circle", { cx, cy, r: rf, class: "frame" }), el("circle", { cx, cy, r: rd, class: "leaf" }));
  for (let i = 0; i < 30; i++) {
    const a = -Math.PI / 2 + (i * 2 * Math.PI) / 30;
    const L = pkHash ? 0.15 + 0.85 * (pkHash[i] / 255) : 0.1;
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

function option(value, text) {
  const o = document.createElement("option");
  o.value = value;
  o.textContent = text;
  return o;
}

async function refresh() {
  if (!S.wallet) return;
  const solBal = BigInt(await conn.getBalance(S.wallet.publicKey, "confirmed"));
  $("wallet-sol").textContent = fmtUnits(solBal, 9) + " SOL";
  S.walletTokens = await tokenAccounts(S.wallet.publicKey);
  $("dep-asset").replaceChildren(option("SOL", `SOL (wallet: ${fmtUnits(solBal, 9)})`),
    ...S.walletTokens.filter((t) => t.amount > 0n).map((t) =>
      option(t.address.toString(), `${tokenLabel(t.mint)} (wallet: ${fmtUnits(t.amount, t.decimals)})`)));

  const hasVault = !!S.seed;
  $("no-vault").hidden = hasVault;
  $("vault").hidden = !hasVault;
  if (!hasVault) { drawDoor(null); return; }

  const k = key();
  const [vault] = k.vault(PID);
  const st = await vaultState(vault);
  S.vaultState = st;
  drawDoor(k.pkHash());
  $("vault-index").textContent = "#" + S.index;
  $("vault-addr").textContent = vault.toString();
  $("vault-link").href = CONFIG.explorer("address", vault.toString());
  const rows = [];
  if (st) {
    const sol = st.lamports - S.rentKeep;
    rows.push(["SOL", fmtUnits(sol > 0n ? sol : 0n, 9)]);
    S.vaultTokens = await tokenAccounts(vault);
    for (const t of S.vaultTokens) rows.push([tokenLabel(t.mint), fmtUnits(t.amount, t.decimals)]);
    $("send-asset").replaceChildren(option("SOL", "SOL"), ...S.vaultTokens.map((t) => option(t.mint.toString(), tokenLabel(t.mint))));
  }
  $("balances").replaceChildren(...rows.map(([a, v]) => {
    const tr = document.createElement("tr");
    tr.innerHTML = "<th scope=row></th><td></td>";
    tr.children[0].textContent = a;
    tr.children[1].textContent = v;
    return tr;
  }));
  $("vault-state").textContent = !st ? "not opened" : st.status === 2 ? "withdrawal in progress" : "locked";
  $("resume").hidden = !(st && st.status === 2);
  updateFee();
}

function updateFee() {
  try {
    const sel = $("send-asset").value;
    const t = S.vaultTokens.find((x) => x.mint.toString() === sel);
    const d = t ? t.decimals : 9;
    const amt = parseUnits($("send-amount").value, d);
    $("fee").textContent = `Fee 0.1 %: ${fmtUnits(qc.feeFor(amt), d)} ${t ? tokenLabel(t.mint) : "SOL"}`;
  } catch { $("fee").textContent = "Fee: 0.1 % of the amount sent"; }
}

function showRecovery(fresh = false) {
  $("recovery-code").textContent = qc.toHex(S.seed);
  $("recovery").hidden = false;
  $("recovery-fresh").hidden = !fresh;
}

function downloadBackup() {
  const blob = new Blob([`QVault recovery code (devnet)\n${qc.toHex(S.seed)}\nProgram: ${PID}\n`], { type: "text/plain" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "qvault-recovery-code.txt";
  a.click();
  URL.revokeObjectURL(a.href);
}

function copy(id) {
  navigator.clipboard?.writeText($(id).textContent).catch(() => {});
}

function bind() {
  drawDoor(null);
  $("phantom-btn").onclick = () => run(connectPhantom);
  $("demo-btn").onclick = () => run(useDemoWallet);
  $("wallet-btn").onclick = () => { if (!S.wallet) $("connect").scrollIntoView({ behavior: "smooth" }); };
  $("airdrop-btn").onclick = () => run(airdrop);
  $("mint-btn").onclick = mintTestTokens;
  $("create-btn").onclick = createVault;
  $("restore-btn").onclick = restoreVault;
  $("dep-form").onsubmit = (e) => { e.preventDefault(); deposit(); };
  $("send-form").onsubmit = (e) => { e.preventDefault(); withdraw(); };
  $("resume").onclick = () => withdraw(true);
  $("show-code").onclick = () => showRecovery(false);
  $("hide-code").onclick = () => { $("recovery").hidden = true; };
  $("download-code").onclick = downloadBackup;
  $("copy-vault").onclick = () => copy("vault-addr");
  $("copy-wallet").onclick = () => copy("wallet-addr");
  $("send-amount").oninput = updateFee;
  $("send-asset").onchange = updateFee;
  $("phantom-hint").hidden = !!phantomProvider();
}

bind();

#!/usr/bin/env python3
"""
BullOne.ai Daily Bot v4 — Fixed version

Fixes from v3:
1. Unstake: auto-find available withdrawal ID (0,1,2... try until gas estimate passes)
2. DEX trade: try multiple nonces (0,1,50,100) to skip "nonce too low"
3. Claim: retry with longer delays (60s, 120s, 300s) for backend indexing lag
4. Pay chain: skip createPayee (needs account on pay chain first), try transfer only
5. Better error handling and reporting
"""

import requests, json, time, uuid, struct, sys, os
from pathlib import Path

# Load .env file if present
_ENV_FILE = Path(__file__).parent / ".env"
if _ENV_FILE.exists():
    with open(_ENV_FILE) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                os.environ.setdefault(key.strip(), val.strip())

from eth_account import Account
from eth_account.messages import encode_defunct, encode_typed_data
from web3 import Web3
from eth_abi import encode as abi_encode

sys.path.insert(0, '/usr/local/lib/hermes-agent/venv/lib/python3.11/site-packages')
import msgpack

# === CONFIG ===
# Single PK or comma-separated for multi-account
WALLET_PKS = [pk.strip() for pk in os.environ.get("BULLONE_PKS", os.environ.get("BULLONE_PK", "")).split(",") if pk.strip()]
BULLONE = "https://www.bullone.ai"
DEX_API = "https://dex-ui-api.bullink.com"
CORE_RPC = "https://core-testnet-rpc.bullink.com"
SEPOLIA_RPC = "https://11155111.rpc.thirdweb.com"

CHAIN_IDS = {"sepolia": 11155111, "core": 10688, "spot": 10699, "pay": 10711}
GATEWAY_ID = 1

# Contract addresses
STAKING_PRECOMPILE = "0x0000000000000000000000000000000000001000"
BRIDGE_PRECOMPILE = "0x0000000000000000000000000000000000001003"
USDT_CORE = "0x103E4B36bcaC55dfeD2Ba8c8eCF36daBfC75E1f7"
ETH_BRIDGE_SEPOLIA = "0xE4352Dcc13531D256824f5B1C8Cc8F517A432144"
USDT_BRIDGE_SEPOLIA = "0x510DE08D4b3388EC81AA116324C9aca2c8c757Bb"
SEPOLIA_USDT = "0xc98107ADB8fbB66B94dcb780EDd3B6Db8827B45e"
ZERO_ADDR = "0x0000000000000000000000000000000000000000"

# These are set per-wallet in run_wallet()
acct = None
WALLET_ADDR = None


def send_raw(w3, tx_dict):
    """Sign and send TX, return receipt."""
    signed = acct.sign_transaction(tx_dict)
    raw = getattr(signed, 'raw_transaction', None) or getattr(signed, 'rawTransaction', None)
    tx_hash = w3.eth.send_raw_transaction(raw)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    time.sleep(2)  # avoid nonce race
    return receipt


def to_hex_32(b):
    """Convert bytes to 0x-prefixed hex string (handles HexBytes that already has 0x)."""
    h = b.hex()
    if h.startswith("0x"):
        return "0x" + h[2:].zfill(64)
    return "0x" + h.zfill(64)


def auth_session():
    """Authenticate with BullOne API"""
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json", "Origin": BULLONE})
    r = s.post(f"{BULLONE}/api/auth/wallet/nonce", json={"walletAddress": WALLET_ADDR})
    nd = r.json()
    sig = acct.sign_message(encode_defunct(text=nd["data"]["message"])).signature.hex()
    if not sig.startswith("0x"): sig = "0x" + sig
    r = s.post(f"{BULLONE}/api/auth/wallet/verify", json={
        "walletAddress": WALLET_ADDR, "challengeId": nd["data"]["challengeId"], "signature": sig
    })
    return s


def get_task_map(s):
    """Get all tasks as {taskKey: task_dict}. Includes claimStatus and recurrence."""
    r = s.get(f"{BULLONE}/api/campaign/tasks")
    tasks = r.json().get("data", {}).get("tasks", [])
    return {t.get("taskKey", ""): t for t in tasks}


def should_run(task_map, task_key):
    """Check if a task should be executed.
    - one_time + done → skip (already completed permanently)
    - one_time + go/claim → run (not yet completed)
    - daily + done → run (resets each day, need to redo)
    - daily + go/claim → run (not yet done today)
    """
    t = task_map.get(task_key)
    if not t:
        return True  # unknown task, try anyway
    status = t.get("claimStatus", "")
    recurrence = t.get("recurrence", "")
    if status == "done" and recurrence == "one_time":
        return False  # permanently done, skip
    return True  # all other cases: run


def check_in(s):
    """Daily check-in"""
    r = s.post(f"{BULLONE}/api/campaign/check-in")
    data = r.json()
    if data.get("ok"):
        print(f"✅ Check-in: +{data.get('data',{}).get('pointsAwarded',0)} BP")
    else:
        print(f"Check-in: {data.get('error',{}).get('code','?')}")


def claim_task(s, task_key):
    """Claim a task by taskKey"""
    r = s.post(f"{BULLONE}/api/campaign/tasks/{task_key}/claim",
               headers={"Idempotency-Key": str(uuid.uuid4())})
    data = r.json()
    if data.get("ok"):
        awarded = data.get("data",{}).get("claim",{}).get("pointsAwarded",0)
        print(f"  ✅ {task_key}: +{awarded} BP")
        return True
    else:
        err = data.get("error",{}).get("message", str(data.get("error","?")))
        print(f"  ❌ {task_key}: {err[:80]}")
        return False


def claim_all(s):
    """Try to claim all tasks (both 'go' and 'claim' status), return total BP"""
    r = s.get(f"{BULLONE}/api/campaign/tasks")
    tasks = r.json().get("data",{}).get("tasks",[])
    claimed = 0
    for t in tasks:
        status = t.get("claimStatus", t.get("status", ""))
        if status in ("go", "claim"):
            if claim_task(s, t.get("taskKey", "")):
                claimed += 1
    r = s.get(f"{BULLONE}/api/campaign/me")
    points = r.json().get("data",{}).get("profile",{}).get("points", 0)
    print(f"  Claimed {claimed} tasks. Total BP: {points}")
    return points


def get_pending_tasks(s):
    """Get list of tasks with status=go or claim"""
    r = s.get(f"{BULLONE}/api/campaign/tasks")
    tasks = r.json().get("data",{}).get("tasks",[])
    return [t for t in tasks if t.get("claimStatus", t.get("status","")) in ("go", "claim")]


def stake_bg(w3):
    """Stake tBULL — delegate(uint64) payable to staking precompile"""
    # Check if already staked
    get_del_sel = w3.keccak(text="getDelegator(uint64,address)")[:4]
    del_data = get_del_sel + abi_encode(["uint64", "address"], [1, WALLET_ADDR])
    result = w3.eth.call({"to": STAKING_PRECOMPILE, "data": del_data})
    fields = [int.from_bytes(result[i:i+32], "big") for i in range(0, len(result), 32)]
    if fields[0] > 0:
        print(f"  Stake: skip (already staked {w3.from_wei(fields[0], 'ether')} ETH)")
        return True
    
    nonce = w3.eth.get_transaction_count(WALLET_ADDR)
    selector = w3.keccak(text="delegate(uint64)")[:4]
    calldata = selector + abi_encode(['uint64'], [1])
    gas_price = w3.eth.gas_price
    tx = {
        "from": WALLET_ADDR, "to": STAKING_PRECOMPILE, "data": calldata,
        "value": w3.to_wei(0.001, "ether"),
        "nonce": nonce, "gas": 500000, "gasPrice": gas_price,
        "chainId": w3.eth.chain_id,
    }
    receipt = send_raw(w3, tx)
    print(f"  Stake: {'✅' if receipt.status == 1 else '❌'}")
    return receipt.status == 1


def claim_rewards(w3):
    """Claim staking rewards — claimRewards(uint64)"""
    # Check if stake exists
    get_del_sel = w3.keccak(text="getDelegator(uint64,address)")[:4]
    del_data = get_del_sel + abi_encode(["uint64", "address"], [1, WALLET_ADDR])
    result = w3.eth.call({"to": STAKING_PRECOMPILE, "data": del_data})
    fields = [int.from_bytes(result[i:i+32], "big") for i in range(0, len(result), 32)]
    if fields[0] == 0:
        print("  Claim rewards: skip (no active stake)")
        return False
    
    nonce = w3.eth.get_transaction_count(WALLET_ADDR)
    selector = w3.keccak(text="claimRewards(uint64)")[:4]
    calldata = selector + abi_encode(['uint64'], [1])
    gas_price = w3.eth.gas_price
    tx = {
        "from": WALLET_ADDR, "to": STAKING_PRECOMPILE, "data": calldata,
        "nonce": nonce, "gas": 500000, "gasPrice": gas_price,
        "chainId": w3.eth.chain_id,
    }
    receipt = send_raw(w3, tx)
    print(f"  Claim rewards: {'✅' if receipt.status == 1 else '❌'}")
    return receipt.status == 1


def unstake_bg(w3):
    """Unstake — undelegate(uint64,uint256,uint8)
    FIX v5: Auto-find available withdrawal ID, skip if deltaStake > 0 (already pending).
    Also checks deltaStake — if non-zero, unstake already submitted, skip to avoid duplicate.
    """
    # Check if stake exists
    get_del_sel = w3.keccak(text="getDelegator(uint64,address)")[:4]
    del_data = get_del_sel + abi_encode(["uint64", "address"], [1, WALLET_ADDR])
    result = w3.eth.call({"to": STAKING_PRECOMPILE, "data": del_data})
    fields = [int.from_bytes(result[i:i+32], "big") for i in range(0, len(result), 32)]
    stake_amount = fields[0]
    delta_stake = fields[3] if len(fields) > 3 else 0

    print(f"  Stake: {stake_amount} ({stake_amount/10**18 if stake_amount else 0} tBULL), deltaStake: {delta_stake}")

    if stake_amount == 0 and delta_stake == 0:
        print("  Unstake: skip (no active stake and no pending unstake)")
        return False

    if delta_stake > 0:
        print(f"  Unstake: skip (unstake already pending, deltaStake={delta_stake})")
        return False

    if stake_amount == 0:
        print("  Unstake: skip (stake already withdrawn)")
        return False

    # Find available withdrawal ID
    selector = w3.keccak(text="undelegate(uint64,uint256,uint8)")[:4]
    gas_price = w3.eth.gas_price
    for wid in range(20):
        calldata = selector + abi_encode(['uint64', 'uint256', 'uint8'], [1, stake_amount, wid])
        try:
            gas_est = w3.eth.estimate_gas({
                "from": WALLET_ADDR, "to": STAKING_PRECOMPILE, "data": calldata,
                "nonce": w3.eth.get_transaction_count(WALLET_ADDR),
                "gasPrice": gas_price, "chainId": w3.eth.chain_id,
            })
            # Gas estimate passed — this wid is available
            nonce = w3.eth.get_transaction_count(WALLET_ADDR)
            tx = {
                "from": WALLET_ADDR, "to": STAKING_PRECOMPILE, "data": calldata,
                "nonce": nonce, "gas": max(gas_est + 20000, 200000),
                "gasPrice": gas_price,
                "chainId": w3.eth.chain_id,
            }
            receipt = send_raw(w3, tx)
            print(f"  Unstake (wid={wid}, gas={gas_est}): {'✅' if receipt.status == 1 else '❌'} TX={receipt.transactionHash.hex()[:20]}...")
            return receipt.status == 1
        except Exception as e:
            err = str(e)
            if "withdrawal id exists" in err or "0x7769746864726177616c" in err:
                continue  # try next wid
            else:
                print(f"  Unstake wid={wid}: error — {err[:150]}")
                continue

    print("  Unstake: ❌ no available withdrawal ID (0-19 all used)")
    return False


def bridge_core_to_chain(w3, target_chain_id):
    """Bridge from Core to Spot/Pay chain"""
    nonce = w3.eth.get_transaction_count(WALLET_ADDR)
    selector = w3.keccak(text="depositTo(address,uint64)")[:4]
    calldata = selector + abi_encode(['address', 'uint64'], [WALLET_ADDR, target_chain_id])
    gas_price = w3.eth.gas_price
    tx = {
        "from": WALLET_ADDR, "to": BRIDGE_PRECOMPILE, "data": calldata,
        "value": w3.to_wei(0.001, "ether"),
        "nonce": nonce, "gas": 500000, "gasPrice": gas_price,
        "chainId": w3.eth.chain_id,
    }
    receipt = send_raw(w3, tx)
    chain_name = "Spot" if target_chain_id == CHAIN_IDS["spot"] else "Pay"
    print(f"  Bridge to {chain_name}: {'✅' if receipt.status == 1 else '❌'}")
    return receipt.status == 1


def bridge_sepolia_to_core(w3, sepolia_w3):
    """Bridge ETH from Sepolia to Core — uses depositTo on Core bridge precompile.
    The bridge_eth_to_core task detects ETH deposited via Core chain bridge.
    """
    core_bal = w3.eth.get_balance(WALLET_ADDR)
    if core_bal < w3.to_wei(0.0005, "ether"):
        print(f"  Sepolia bridge: skip (insufficient Core ETH: {w3.from_wei(core_bal, 'ether')})")
        return False

    nonce = w3.eth.get_transaction_count(WALLET_ADDR)
    selector = w3.keccak(text="depositTo(address,uint64)")[:4]
    # Bridge to Spot chain (10699) — only Spot and Pay supported as targets
    calldata = selector + abi_encode(['address', 'uint64'], [WALLET_ADDR, CHAIN_IDS["spot"]])
    gas_price = w3.eth.gas_price
    tx = {
        "from": WALLET_ADDR, "to": BRIDGE_PRECOMPILE, "data": calldata,
        "value": w3.to_wei(0.0001, "ether"),
        "nonce": nonce, "gas": 500000, "gasPrice": gas_price,
        "chainId": w3.eth.chain_id,
    }
    receipt = send_raw(w3, tx)
    print(f"  Bridge ETH→Core: {'✅' if receipt.status == 1 else '❌'} TX={receipt.transactionHash.hex()[:20]}...")
    return receipt.status == 1


def dex_place_order(s):
    """Place order on DEX via EIP-712 ApproveAgent + placeOrder (working v3 approach).
    Uses agent wallet to sign placeOrder, main wallet to sign approveAgent.
    """
    agent_acct = Account.create()

    # Step 1: Approve agent
    nonce = int(time.time() * 1000)
    valid_until = nonce + AGENT_VALIDITY_MS
    agent_name = f"bullone.ai valid_until {valid_until}"
    sig_chain_id = hex(CHAIN_IDS["spot"])

    action = {
        "type": "approveAgent",
        "agentAddress": agent_acct.address,
        "agentName": agent_name,
        "nonce": nonce,
        "signatureChainId": sig_chain_id,
    }

    typed_data = {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"}, {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"}, {"name": "verifyingContract", "type": "address"},
            ],
            "SpotTransaction:ApproveAgent": [
                {"name": "agentAddress", "type": "address"}, {"name": "agentName", "type": "string"},
                {"name": "nonce", "type": "uint64"}, {"name": "gatewayId", "type": "uint32"},
            ],
        },
        "primaryType": "SpotTransaction:ApproveAgent",
        "domain": {"name": "Spot-Exchange", "version": "1", "chainId": CHAIN_IDS["spot"], "verifyingContract": ZERO_ADDR},
        "message": {"agentAddress": agent_acct.address, "agentName": agent_name, "nonce": nonce, "gatewayId": GATEWAY_ID},
    }

    signed = acct.sign_message(encode_typed_data(full_message=typed_data))
    sig_hex = signed.signature.hex()
    if not sig_hex.startswith("0x"): sig_hex = "0x" + sig_hex
    sig = parse_sig(sig_hex)

    body = {
        "action": action, "nonce": nonce, "signature": sig,
        "vaultAddress": None, "expiresAfter": None, "isFrontend": True,
    }

    r = s.post(f"{BULLONE}/api/trade/gateway-sponsor", json=body, timeout=15)
    if r.status_code != 200:
        print(f"  DEX: gateway-sponsor failed {r.status_code} - {r.text[:150]}")
        return False
    body["gateway_sponsor"] = r.json()["data"]["gateway_sponsor"]

    r2 = requests.post(f"{DEX_API}/exchange", json=body,
                       headers={"Content-Type": "application/json", "Accept": "application/json"}, timeout=15)
    if r2.status_code != 200:
        print(f"  DEX approveAgent: {r2.status_code} - {r2.text[:150]}")
        return False
    print(f"  DEX: agent approved")
    time.sleep(2)

    # Step 2: Get market info
    dex_s = requests.Session()
    dex_s.headers.update({"Content-Type": "application/json", "Accept": "application/json"})
    r = dex_s.post(f"{DEX_API}/info", json={"type": "spotMeta"}, timeout=15)
    markets = r.json().get("universe", [])
    bull_market = next((m for m in markets if m.get("name") == "tBULL/tUSDT"), None)
    if not bull_market:
        print("  DEX: tBULL/tUSDT market not found")
        return False

    r2 = dex_s.post(f"{DEX_API}/info", json={"type": "allMids"}, timeout=15)
    mids = r2.json()
    price = float(mids.get(f"@{bull_market['index']}", "0"))
    if price == 0:
        print("  DEX: no market price, using 0.05")
        price = 0.05

    # Step 3: Place order — buy 1 tBULL, price 10% above market, tick-aligned
    import math
    buy_price_chain = int(math.ceil(price * 1.1 * 1e6 / 10) * 10)
    buy_price = str(buy_price_chain / 1e6)

    order = {
        "a": bull_market["index"], "b": True, "p": buy_price, "s": "1",
        "r": False, "t": {"limit": {"tif": "Gtc"}},
    }
    action2 = {"type": "placeOrder", "orders": [order], "grouping": "na"}

    nonce2 = int(time.time() * 1000)
    connection_id = create_gateway_action_hash(action2, nonce2)

    typed_data2 = {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"}, {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"}, {"name": "verifyingContract", "type": "address"},
            ],
            "Agent": [
                {"name": "source", "type": "string"}, {"name": "connectionId", "type": "bytes32"},
            ],
        },
        "primaryType": "Agent",
        "domain": {"name": "Spot-Exchange", "version": "1", "chainId": CHAIN_IDS["spot"], "verifyingContract": ZERO_ADDR},
        "message": {"source": "b", "connectionId": connection_id},
    }

    signed2 = agent_acct.sign_message(encode_typed_data(full_message=typed_data2))
    sig_hex2 = signed2.signature.hex()
    if not sig_hex2.startswith("0x"): sig_hex2 = "0x" + sig_hex2
    sig2 = parse_sig(sig_hex2)

    body2 = {
        "action": action2, "nonce": nonce2, "signature": sig2,
        "vaultAddress": None, "expiresAfter": None, "isFrontend": True,
    }

    r3 = s.post(f"{BULLONE}/api/trade/gateway-sponsor", json=body2, timeout=15)
    if r3.status_code != 200:
        print(f"  DEX order: gateway-sponsor failed {r3.status_code}")
        return False
    body2["gateway_sponsor"] = r3.json()["data"]["gateway_sponsor"]

    r4 = requests.post(f"{DEX_API}/exchange", json=body2,
                       headers={"Content-Type": "application/json", "Accept": "application/json"}, timeout=15)
    if r4.status_code == 200:
        print(f"  DEX placeOrder: ✅ buy 1 tBULL @ {buy_price}")
        return True
    else:
        print(f"  DEX placeOrder: {r4.status_code} - {r4.text[:150]}")
        return False


# DEX helper functions (from dex_trade_v3.py)
AGENT_VALIDITY_MS = 77760000

def parse_sig(sig_hex):
    if not sig_hex.startswith("0x"): sig_hex = "0x" + sig_hex
    r = "0x" + sig_hex[2:66]
    s = "0x" + sig_hex[66:130]
    v_raw = int(sig_hex[130:132], 16)
    v = v_raw if v_raw >= 27 else v_raw + 27
    return {"r": r, "s": s, "v": v}

def create_gateway_action_hash(action, nonce, gateway_id=GATEWAY_ID):
    norm = dict(action)
    if "agentAddress" in norm:
        norm["agentAddress"] = norm["agentAddress"].lower()
    packed = msgpack.packb(norm, use_bin_type=True)
    nonce_bytes = nonce.to_bytes(8, byteorder="big")
    gw_bytes = gateway_id.to_bytes(4, byteorder="big")
    combined = packed + nonce_bytes + gw_bytes + bytes([1])
    return Web3.keccak(combined).hex()


def pay_chain_transfer():
    """Pay chain transfer via EIP-712 — transfer to payee (bech32 address).
    
    Fixed: "to" field must be bytes type (pay chain uses bech32 addresses, not eth addresses).
    Token: USDT on pay chain (0x103E4B36bcaC55dfeD2Ba8c8eCF36daBfC75E1f7).
    Amount: 1,000,000 (1 USDT, 6 decimals) — must be above payee's min_payment_amount.
    
    Prerequisites: need USDT bridged to pay chain first (via bridge_to_pay_chain task).
    """
    # Known payee address (bech32) — this wallet's payee on pay chain
    PAYEE = "pp1h0p9jfvwgq5elnz79vqthep8t8k77xfpr0rvuw"
    TOKEN = USDT_CORE  # USDT token address
    AMOUNT = 1000000   # 1 USDT (6 decimals)

    DOMAIN = {"name": "PayChain", "version": "1", "chainId": CHAIN_IDS["pay"], "verifyingContract": ZERO_ADDR}
    EIP712 = [{"name":"name","type":"string"},{"name":"version","type":"string"},
              {"name":"chainId","type":"uint256"},{"name":"verifyingContract","type":"address"}]
    TYPES = {
        "EIP712Domain": EIP712,
        "TransferEntry": [{"name":"to","type":"bytes"},{"name":"amount","type":"uint128"},{"name":"memo","type":"bytes"}],
        "Transfer": [{"name":"nonce","type":"uint64"},{"name":"token","type":"address"},{"name":"entries","type":"TransferEntry[]"}],
    }

    # Get nonce
    r = requests.post(f"{BULLONE}/api/proxy/pay-rpc",
        json={"jsonrpc":"2.0","id":1,"method":"pay_getTransactionCount","params":[WALLET_ADDR, "latest"]},
        headers={"Content-Type": "application/json"})
    nonce_val = int(r.json().get("result", "0x0"), 16)

    # Encode payee as bytes (bech32 string → bytes)
    payee_bytes = PAYEE.encode()

    msg = {"nonce": nonce_val, "token": TOKEN,
           "entries": [{"to": payee_bytes, "amount": AMOUNT, "memo": b""}]}
    full = {"types": TYPES, "primaryType": "Transfer", "domain": DOMAIN, "message": msg}
    signable = encode_typed_data(full_message=full)
    signed = acct.sign_message(signable)
    sig = parse_sig(signed.signature.hex())

    action = {"type": "transfer", "token": TOKEN,
              "entries": [{"to": PAYEE, "amount": hex(AMOUNT), "memo": "0x"}]}
    params = {"nonce": nonce_val, "action": action, "signature": sig}

    r = requests.post(f"{BULLONE}/api/proxy/pay-rpc",
        json={"jsonrpc":"2.0","id":1,"method":"pay_sendTransaction","params":[params]},
        headers={"Content-Type": "application/json"})
    resp = r.json()
    success = resp.get("result") is not None
    err = resp.get("error", {}).get("message", "") if not success else ""
    print(f"  Pay transfer: {'✅' if success else '❌ ' + err[:80]}")
    return success


def pay_refund_order():
    """Payee Refund — refund a received payment order via EIP-712 Refund action.
    
    Flow:
    1. Check if payee account exists via pay_getPayee
    2. Search for refundable orders (to=payee, refunded=false)
    3. Refund the first refundable order via pay_sendTransaction
    
    Prerequisites: payee account must be created first (create_payee_account task).
    """
    PAYEE = "pp1h0p9jfvwgq5elnz79vqthep8t8k77xfpr0rvuw"
    
    DOMAIN = {"name": "PayChain", "version": "1", "chainId": CHAIN_IDS["pay"], "verifyingContract": ZERO_ADDR}
    EIP712 = [{"name":"name","type":"string"},{"name":"version","type":"string"},
              {"name":"chainId","type":"uint256"},{"name":"verifyingContract","type":"address"}]
    
    # 1. Check payee exists
    r = requests.post(f"{BULLONE}/api/proxy/pay-rpc",
        json={"jsonrpc":"2.0","id":1,"method":"pay_getPayee","params":[WALLET_ADDR, "latest"]},
        headers={"Content-Type":"application/json"})
    payee = r.json().get("result")
    if not payee:
        print("  Payee refund: skip (no payee account)")
        return False
    
    # 2. Search for refundable orders (check a range of recent order IDs)
    r2 = requests.post(f"{BULLONE}/api/proxy/pay-rpc",
        json={"jsonrpc":"2.0","id":1,"method":"pay_getTransactionCount","params":[WALLET_ADDR, "latest"]},
        headers={"Content-Type":"application/json"})
    nonce_val = int(r2.json().get("result", "0x0"), 16)
    
    # Estimate order ID range from nonce
    order_id = 152384  # Known successful order ID as reference
    refundable = None
    
    # Try a range of order IDs
    for oid in range(order_id - 20, order_id + 50):
        r3 = requests.post(f"{BULLONE}/api/proxy/pay-rpc",
            json={"jsonrpc":"2.0","id":1,"method":"pay_getOrder","params":[oid]},
            headers={"Content-Type":"application/json"})
        result = r3.json().get("result", {})
        if result and result.get("to") == PAYEE and not result.get("refunded"):
            refundable = result
            break
    
    if not refundable:
        print("  Payee refund: skip (no refundable orders)")
        return False
    
    # 3. Refund the order
    TYPES = {
        "EIP712Domain": EIP712,
        "Refund": [{"name":"nonce","type":"uint64"},{"name":"orderId","type":"uint64"}],
    }
    
    msg = {"nonce": nonce_val, "orderId": int(refundable["order_id"], 16)}
    full = {"types": TYPES, "primaryType": "Refund", "domain": DOMAIN, "message": msg}
    signable = encode_typed_data(full_message=full)
    signed = acct.sign_message(signable)
    sig = parse_sig(signed.signature.hex())
    
    action = {"type": "refund", "orderId": int(refundable["order_id"], 16)}
    params = {"nonce": nonce_val, "action": action, "signature": sig}
    
    r4 = requests.post(f"{BULLONE}/api/proxy/pay-rpc",
        json={"jsonrpc":"2.0","id":1,"method":"pay_sendTransaction","params":[params]},
        headers={"Content-Type":"application/json"})
    resp = r4.json()
    success = resp.get("result") is not None
    err = resp.get("error", {}).get("message", "") if not success else ""
    print(f"  Payee refund (order {int(refundable['order_id'], 16)}): {'✅' if success else '❌ ' + err[:80]}")
    return success


def pay_standard_transfer(session):
    """Standard transfer via pay chain — single entry to wallet address (hex)."""
    TOKEN = USDT_CORE  # USDT on pay chain
    AMOUNT = 1000000   # 1 USDT

    DOMAIN = {"name": "PayChain", "version": "1", "chainId": CHAIN_IDS["pay"], "verifyingContract": ZERO_ADDR}
    EIP712 = [{"name":"name","type":"string"},{"name":"version","type":"string"},
              {"name":"chainId","type":"uint256"},{"name":"verifyingContract","type":"address"}]
    TYPES = {
        "EIP712Domain": EIP712,
        "TransferEntry": [{"name":"to","type":"bytes"},{"name":"amount","type":"uint128"},{"name":"memo","type":"bytes"}],
        "Transfer": [{"name":"nonce","type":"uint64"},{"name":"token","type":"address"},{"name":"entries","type":"TransferEntry[]"}],
    }

    r = session.post(f"{BULLONE}/api/proxy/pay-rpc",
        json={"jsonrpc":"2.0","id":1,"method":"pay_getTransactionCount","params":[WALLET_ADDR, "latest"]},
        headers={"Content-Type": "application/json"})
    nonce_val = int(r.json().get("result", "0x0"), 16)

    # Destination: self (standard transfer to own wallet address)
    dest_bytes = bytes.fromhex(WALLET_ADDR[2:])

    msg = {"nonce": nonce_val, "token": TOKEN,
           "entries": [{"to": dest_bytes, "amount": AMOUNT, "memo": b""}]}
    full = {"types": TYPES, "primaryType": "Transfer", "domain": DOMAIN, "message": msg}
    signable = encode_typed_data(full_message=full)
    signed = acct.sign_message(signable)
    sig = parse_sig(signed.signature.hex())

    action = {"type": "transfer", "token": TOKEN,
              "entries": [{"to": WALLET_ADDR, "amount": hex(AMOUNT), "memo": "0x"}]}
    params = {"nonce": nonce_val, "action": action, "signature": sig}

    r = session.post(f"{BULLONE}/api/proxy/pay-rpc",
        json={"jsonrpc":"2.0","id":1,"method":"pay_sendTransaction","params":[params]},
        headers={"Content-Type": "application/json"})
    resp = r.json()
    success = resp.get("result") is not None
    err = resp.get("error", {}).get("message", "") if not success else ""
    print(f"  Standard transfer: {'✅' if success else '❌ ' + err[:80]}")
    return success


def pay_batch_transfer(session):
    """Batch transfer via pay chain — multiple entries in one transaction."""
    TOKEN = USDT_CORE
    AMOUNT = 100000  # 0.1 USDT per entry
    NUM_ENTRIES = 3

    DOMAIN = {"name": "PayChain", "version": "1", "chainId": CHAIN_IDS["pay"], "verifyingContract": ZERO_ADDR}
    EIP712 = [{"name":"name","type":"string"},{"name":"version","type":"string"},
              {"name":"chainId","type":"uint256"},{"name":"verifyingContract","type":"address"}]
    TYPES = {
        "EIP712Domain": EIP712,
        "TransferEntry": [{"name":"to","type":"bytes"},{"name":"amount","type":"uint128"},{"name":"memo","type":"bytes"}],
        "Transfer": [{"name":"nonce","type":"uint64"},{"name":"token","type":"address"},{"name":"entries","type":"TransferEntry[]"}],
    }

    r = session.post(f"{BULLONE}/api/proxy/pay-rpc",
        json={"jsonrpc":"2.0","id":1,"method":"pay_getTransactionCount","params":[WALLET_ADDR, "latest"]},
        headers={"Content-Type": "application/json"})
    nonce_val = int(r.json().get("result", "0x0"), 16)

    dest_bytes = bytes.fromhex(WALLET_ADDR[2:])
    entries = [{"to": dest_bytes, "amount": AMOUNT, "memo": b""} for _ in range(NUM_ENTRIES)]

    msg = {"nonce": nonce_val, "token": TOKEN, "entries": entries}
    full = {"types": TYPES, "primaryType": "Transfer", "domain": DOMAIN, "message": msg}
    signable = encode_typed_data(full_message=full)
    signed = acct.sign_message(signable)
    sig = parse_sig(signed.signature.hex())

    action_entries = [{"to": WALLET_ADDR, "amount": hex(AMOUNT), "memo": "0x"} for _ in range(NUM_ENTRIES)]
    action = {"type": "transfer", "token": TOKEN, "entries": action_entries}
    params = {"nonce": nonce_val, "action": action, "signature": sig}

    r = session.post(f"{BULLONE}/api/proxy/pay-rpc",
        json={"jsonrpc":"2.0","id":1,"method":"pay_sendTransaction","params":[params]},
        headers={"Content-Type": "application/json"})
    resp = r.json()
    success = resp.get("result") is not None
    err = resp.get("error", {}).get("message", "") if not success else ""
    print(f"  Batch transfer ({NUM_ENTRIES}x): {'✅' if success else '❌ ' + err[:80]}")
    return success


def payee_receive_payment(session):
    """Receive payment via payee account — transfer to self as payee (bech32)."""
    PAYEE = "pp1h0p9jfvwgq5elnz79vqthep8t8k77xfpr0rvuw"
    TOKEN = USDT_CORE
    AMOUNT = 1000000  # 1 USDT

    DOMAIN = {"name": "PayChain", "version": "1", "chainId": CHAIN_IDS["pay"], "verifyingContract": ZERO_ADDR}
    EIP712 = [{"name":"name","type":"string"},{"name":"version","type":"string"},
              {"name":"chainId","type":"uint256"},{"name":"verifyingContract","type":"address"}]
    TYPES = {
        "EIP712Domain": EIP712,
        "TransferEntry": [{"name":"to","type":"bytes"},{"name":"amount","type":"uint128"},{"name":"memo","type":"bytes"}],
        "Transfer": [{"name":"nonce","type":"uint64"},{"name":"token","type":"address"},{"name":"entries","type":"TransferEntry[]"}],
    }

    r = session.post(f"{BULLONE}/api/proxy/pay-rpc",
        json={"jsonrpc":"2.0","id":1,"method":"pay_getTransactionCount","params":[WALLET_ADDR, "latest"]},
        headers={"Content-Type": "application/json"})
    nonce_val = int(r.json().get("result", "0x0"), 16)

    payee_bytes = PAYEE.encode()

    msg = {"nonce": nonce_val, "token": TOKEN,
           "entries": [{"to": payee_bytes, "amount": AMOUNT, "memo": b""}]}
    full = {"types": TYPES, "primaryType": "Transfer", "domain": DOMAIN, "message": msg}
    signable = encode_typed_data(full_message=full)
    signed = acct.sign_message(signable)
    sig = parse_sig(signed.signature.hex())

    action = {"type": "transfer", "token": TOKEN,
              "entries": [{"to": PAYEE, "amount": hex(AMOUNT), "memo": "0x"}]}
    params = {"nonce": nonce_val, "action": action, "signature": sig}

    r = session.post(f"{BULLONE}/api/proxy/pay-rpc",
        json={"jsonrpc":"2.0","id":1,"method":"pay_sendTransaction","params":[params]},
        headers={"Content-Type": "application/json"})
    resp = r.json()
    success = resp.get("result") is not None
    err = resp.get("error", {}).get("message", "") if not success else ""
    print(f"  Payee receive payment: {'✅' if success else '❌ ' + err[:80]}")
    return success


def run_wallet(pk):
    """Run bot for a single wallet."""
    global acct, WALLET_ADDR
    acct = Account.from_key(pk)
    WALLET_ADDR = acct.address

    print(f"\n{'='*50}")
    print(f"=== Wallet: {WALLET_ADDR} ===")
    print(f"{'='*50}")

    # Init Web3
    w3 = Web3(Web3.HTTPProvider(CORE_RPC))
    sepolia = Web3(Web3.HTTPProvider(SEPOLIA_RPC))

    # Auth
    s = auth_session()
    print("✅ Authenticated")

    # Get task status map
    task_map = get_task_map(s)

    # Check-in
    check_in(s)

    # Initial claim
    print("\n--- Initial claim ---")
    claim_all(s)

    # On-chain actions — skip one_time tasks that are already done
    print("\n--- On-chain TXs ---")
    if should_run(task_map, "stake_bg"):
        try: stake_bg(w3)
        except Exception as e: print(f"  Stake error: {e}")
    else:
        print("  Stake: skip (one_time, already done)")

    if should_run(task_map, "claim_staking_rewards"):
        try: claim_rewards(w3)
        except Exception as e: print(f"  Claim rewards error: {e}")
    else:
        print("  Claim rewards: skip (one_time, already done)")

    if should_run(task_map, "unstake_bg"):
        try: unstake_bg(w3)
        except Exception as e: print(f"  Unstake error: {e}")
    else:
        print("  Unstake: skip (one_time, already done)")

    if should_run(task_map, "bridge_to_spot_chain"):
        try: bridge_core_to_chain(w3, CHAIN_IDS["spot"])
        except Exception as e: print(f"  Bridge to Spot error: {e}")
    else:
        print("  Bridge to Spot: skip (one_time, already done)")

    if should_run(task_map, "bridge_to_pay_chain"):
        try: bridge_core_to_chain(w3, CHAIN_IDS["pay"])
        except Exception as e: print(f"  Bridge to Pay error: {e}")
    else:
        print("  Bridge to Pay: skip (one_time, already done)")

    if should_run(task_map, "bridge_eth_to_core") or should_run(task_map, "bridge_usdt_to_core"):
        try: bridge_sepolia_to_core(w3, sepolia)
        except Exception as e: print(f"  Sepolia bridge error: {e}")
    else:
        print("  Sepolia bridge: skip (one_time, already done)")

    if should_run(task_map, "daily_trade"):
        try: dex_place_order(s)
        except Exception as e: print(f"  DEX trade error: {e}")
    else:
        print("  DEX trade: skip (daily, already done today)")

    if should_run(task_map, "daily_payment_to_payee"):
        try: pay_chain_transfer()
        except Exception as e: print(f"  Pay transfer error: {e}")
    else:
        print("  Pay transfer: skip (daily, already done today)")

    if should_run(task_map, "daily_standard_transfer"):
        try: pay_standard_transfer(s)
        except Exception as e: print(f"  Standard transfer error: {e}")
    else:
        print("  Standard transfer: skip (daily, already done today)")

    if should_run(task_map, "daily_batch_transfer"):
        try: pay_batch_transfer(s)
        except Exception as e: print(f"  Batch transfer error: {e}")
    else:
        print("  Batch transfer: skip (daily, already done today)")

    if should_run(task_map, "daily_payee_receive_payment"):
        try: payee_receive_payment(s)
        except Exception as e: print(f"  Receive payment error: {e}")
    else:
        print("  Receive payment: skip (daily, already done today)")

    if should_run(task_map, "payee_refund"):
        try: pay_refund_order()
        except Exception as e: print(f"  Payee refund error: {e}")
    else:
        print("  Payee refund: skip (one_time, already done)")

    # Wait for backend detection (retry with increasing delays)
    for delay, label in [(60, "1min"), (120, "2min"), (300, "5min")]:
        print(f"\n--- Waiting {label} for backend detection ---")
        time.sleep(delay)
        pending = get_pending_tasks(s)
        if not pending:
            print("  All tasks completed!")
            break
        print(f"  {len(pending)} tasks still pending, retrying claim...")
        claim_all(s)

    # Final status
    r = s.get(f"{BULLONE}/api/campaign/me")
    points = r.json().get("data",{}).get("profile",{}).get("points", 0)
    pending = get_pending_tasks(s)
    print(f"\n=== Wallet {WALLET_ADDR[:10]}... — {time.strftime('%Y-%m-%d %H:%M:%S')} ===")
    print(f"Total BP: {points}")
    if pending:
        print(f"Still pending ({len(pending)}):")
        for t in pending:
            print(f"  - {t.get('taskKey', '?')}")
    return points


def main():
    print(f"=== BullOne Daily Bot v4 — {time.strftime('%Y-%m-%d %H:%M:%S')} ===")
    print(f"Wallets: {len(WALLET_PKS)}")

    total_bp = 0
    for i, pk in enumerate(WALLET_PKS, 1):
        print(f"\n[{i}/{len(WALLET_PKS)}] Processing wallet...")
        try:
            bp = run_wallet(pk)
            total_bp += bp
        except Exception as e:
            print(f"  ❌ Wallet error: {e}")

    print(f"\n{'='*50}")
    print(f"All wallets done. Total BP across all: {total_bp}")


if __name__ == "__main__":
    main()

# BullOne Bot

Bot otomatis untuk daily tasks [BullOne.ai](https://www.bullone.ai/campaign?ref=_Hyuzay4PE8r) testnet campaign.

## Tasks (23 total)

### Daily (5)
- daily_trade — DEX spot trade (tBULL/tUSDT)
- daily_standard_transfer — standard transfer via pay chain
- daily_batch_transfer — batch transfer via pay chain
- daily_payment_to_payee — transfer ke payee via EIP-712
- daily_payee_receive_payment — receive payment dari payee

### Weekly (3)
- weekly_boost_stake → reuse stake handler
- weekly_boost_trade → reuse DEX handler
- weekly_boost_payment → reuse pay transfer handler

### Monthly (3)
- monthly_boost_stake → reuse stake handler
- monthly_boost_trade → reuse DEX handler
- monthly_boost_payment → reuse pay transfer handler

### One-Time (12)
- connect_social — connect wallet sosial
- send_connect_wallet — connect wallet
- bridge_eth_to_core — bridge ETH Sepolia → Core
- bridge_usdt_to_core — bridge USDT Sepolia → Core
- bridge_to_pay_chain — bridge Core → Pay chain
- bridge_to_spot_chain — bridge Core → Spot chain
- mint_stbull — mint stBULL
- stake_bg — stake BG token
- claim_staking_rewards — claim staking rewards
- unstake_bg — unstake BG (auto-find withdrawal ID)
- swap_core — swap di Core chain
- add_liquidity — add liquidity
- payee_bind — bind payee
- create_payee_account — create payee account
- payee_withdraw — withdraw dari payee
- payee_refund — refund payee order

### Internal/Faucet
- claim_bg, claim_eth, claim_usdt — auto via claim_all()

### Skipped (social media)
- bind_twitter, bind_discord, follow_official_twitter, join_discord_server, retweet_launch_post

## Setup

```bash
pip install eth-account web3 eth-abi msgpack requests
```

## Config

Semua konfigurasi via environment variable atau `.env`:

```bash
# Required: private key (single) atau comma-separated (multi-account)
BULLONE_PK=0x...
# atau
BULLONE_PKS=0x...,0x...
```

Bisa juga simpan di file `.env`:

```
BULLONE_PK=0x...
```

## Run

```bash
python bullone.py
```

Bot akan otomatis login wallet, check-in, dan menjalankan semua task yang belum selesai.

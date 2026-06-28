# BullOne Bot

Bot otomatis untuk daily tasks [BullOne.ai](https://www.bullone.ai/campaign?ref=_Hyuzay4PE8r) testnet campaign.

## Tasks

- Check-in harian
- Claim points
- Stake BG
- Claim staking rewards
- Unstake BG (auto-find withdrawal ID)
- Bridge ETH ke core chain
- DEX trade (spot chain)
- Pay chain transfer ke payee

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

Bot akan otomatis login wallet, check-in, dan menjalankan semua daily tasks yang belum selesai.

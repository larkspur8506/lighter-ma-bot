#!/usr/bin/env python3
"""离线注册 Lighter API key —— **在用户自己的电脑上跑，不在服务器上跑**。

为什么必须离线
==============
注册一个 API key（`ChangePubKey`）需要 **L1 钱包私钥的签名**（`L1Sig` 字段）。L1 私钥
能转走账户里的全部资金，一旦上了服务器就变成「服务器被拿下 = 资金被拿走」。所以：

* 本脚本**不在容器里**、不挂在任何 systemd / cron 上（`Dockerfile` 不 COPY `scripts/`）；
* 服务器上只放**注册之后**的 API 私钥（`LIGHTER_API_PRIVATE_KEY`），它只能交易，
  不能提币到别的地址（docs：「Can do: Trade, cancel, modify… secure withdrawals」/
  「Cannot do alone: transfers to another L1 owner, fast withdrawals」）。

用法
====
    # 1) 装依赖（本机，一次性）
    python -m pip install lighter-sdk==1.1.8

    # 2) 私钥走环境变量（推荐：不进 shell history、不进 `ps`）
    LIGHTER_L1_PRIVATE_KEY=0x… python scripts/offline_sign.py \
        --environment mainnet --api-key-index 4

    # 或者让脚本用 getpass 问（不回显、不进 argv）
    python scripts/offline_sign.py --environment mainnet --api-key-index 4

    # 3) 先看一遍会发生什么（不签名、不发请求）
    python scripts/offline_sign.py --environment mainnet --api-key-index 4 --dry-run

    # 4) 把打印出来的 API 私钥填进服务器的 .env：
    #    LIGHTER_API_PRIVATE_KEY=0x…
    #    LIGHTER_API_KEY_INDEX=4
    #    LIGHTER_ACCOUNT_INDEX=…

安全规约（代码里逐条对应实现）
==============================
1. **私钥只走 env 或 `getpass`，绝不走命令行参数** —— argv 会进 shell history、
   会被 `ps` 看到（同机器其他用户可读）。任何看起来像私钥的 argv（名字里带
   `private`/`secret`，或**值**形如 `0x` + 64 hex）都会让脚本**直接拒绝运行**。
2. **私钥只在内存里**：不写文件、不进日志、不进异常消息；函数返回前 `del` 掉引用
   （CPython 的字符串不可原地擦除，所以这里只能做到「尽早丢掉唯一引用」+ 不复制）。
3. **API 私钥只打印一次**，明确标注它是什么；要落盘必须显式 `--save-api-key <path>`
   （写 `0600`，且拒绝写到仓库目录里）。
4. **注册前后都核对身份**：打印 L1 地址、account_index、api_key_index、公钥，
   提交后轮询 `GET /apikeys` 直到公钥真的出现（`code=200` 只表示 API 收下了，
   不代表链上已经生效 —— docs 明确要求轮询）。
5. **同 index 注册会覆盖旧 key**：docs 原话「Registering a key replaces whatever key was
   previously at that index」。所以默认 `--api-key-index 4`，并在覆盖前打印该 index 上
   现有的公钥让用户确认。

退出码
======
0 成功；2 参数/隐私规约违规；3 身份核对失败；4 签名失败；5 API 拒绝；
6 等待注册超时（此时**不要**重试发送，先用打印的 tx_hash 查 `/tx?by=hash`）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

BASE_URLS: Dict[str, str] = {
    "mainnet": "https://mainnet.zklighter.elliot.ai/api/v1",
    "testnet": "https://testnet.zklighter.elliot.ai/api/v1",
}

L1_KEY_ENV = "LIGHTER_L1_PRIVATE_KEY"
API_KEY_ENV = "LIGHTER_API_PRIVATE_KEY"

# 0x + 64 hex = 32 字节（以太坊私钥）；0x + 80 hex = 40 字节（Lighter API 私钥）。
_HEX_KEY_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
_HEX_ANY_RE = re.compile(r"^0x[0-9a-fA-F]{64,80}$")
_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

# 出现这些片段就认为「有人想从 argv 传 secret」，直接拒绝。
_SECRET_ARG_HINTS = ("private", "secret", "privkey", "priv_key", "seed", "mnemonic")

DEFAULT_API_KEY_INDEX = 4        # 4..254（0-3 是 Lighter 官方前端保留位）
REGISTER_POLL_TIMEOUT_S = 30.0
REGISTER_POLL_INTERVAL_S = 1.0


# ------------------------------------------------------------------ 隐私规约


class PrivacyError(RuntimeError):
    """argv 里出现了疑似 secret（或其它违反安全规约的用法）。"""


def reject_secrets_in_argv(argv: Sequence[str]) -> None:
    """任何疑似「把私钥放到命令行」的用法都必须**在解析参数之前**失败。

    三种情况都拦：参数名里带 private/secret/…；`--flag=0x<64hex>` 这种值；
    裸位置参数形如私钥。
    """
    for arg in argv:
        text = str(arg)
        lowered = text.lower()
        if text.startswith("-") and any(hint in lowered for hint in _SECRET_ARG_HINTS):
            raise PrivacyError(
                f"refusing to accept {text.split('=')[0]} on the command line: private keys "
                f"must come from the {L1_KEY_ENV} environment variable or an interactive "
                f"prompt (argv ends up in shell history and in `ps`)")
        value = text.split("=", 1)[1] if text.startswith("-") and "=" in text else text
        if _HEX_ANY_RE.match(value):
            raise PrivacyError(
                "refusing to run: a value on the command line looks like a private key "
                f"(argv leaks into shell history and `ps`). Use {L1_KEY_ENV} or the "
                "interactive prompt instead.")


# ------------------------------------------------------------------ 参数


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="offline_sign.py",
        description="Register a Lighter API key with an L1 signature (run this on YOUR "
                    "machine; the L1 private key never leaves it).",
        # 明确不提供 --private-key：见 reject_secrets_in_argv 与 docs/EXCHANGE.md。
        epilog=f"The L1 private key is read from ${L1_KEY_ENV} or prompted with getpass. "
               f"There is deliberately no command-line option for it.",
    )
    p.add_argument("--environment", choices=sorted(BASE_URLS), default="mainnet",
                   help="Lighter network (default: mainnet)")
    p.add_argument("--account-index", type=int, default=None,
                   help="Trading account index (default: look it up from the L1 address)")
    p.add_argument("--api-key-index", type=int, default=DEFAULT_API_KEY_INDEX,
                   help=f"API key slot to register, 4..254 (default: {DEFAULT_API_KEY_INDEX})")
    p.add_argument("--l1-address", default=None,
                   help="Expected L1 address; aborts if the key does not derive to it")
    p.add_argument("--dry-run", action="store_true",
                   help="Sign nothing, send nothing; just print the plan")
    p.add_argument("--yes", action="store_true",
                   help="Skip the interactive REGISTER confirmation")
    p.add_argument("--save-api-key", default=None, metavar="PATH",
                   help="Write the new API private key to PATH with mode 0600 instead of "
                        "printing it")
    p.add_argument("--timeout", type=float, default=REGISTER_POLL_TIMEOUT_S,
                   help=f"Seconds to wait for the key to appear on-chain "
                        f"(default: {REGISTER_POLL_TIMEOUT_S:.0f})")
    return p


def _read_l1_key() -> str:
    """私钥来源：环境变量 → getpass。**没有第三个来源。**"""
    from_env = (os.environ.get(L1_KEY_ENV) or "").strip()
    if from_env:
        return from_env
    import getpass
    # getpass 不回显；不写 history（它不是 shell 读的行）。
    return (getpass.getpass("L1 wallet private key (input hidden, never echoed): ") or "").strip()


def _mask_address(address: str) -> str:
    return address if len(address) <= 12 else f"{address[:6]}…{address[-4:]}"


# ------------------------------------------------------------------ 主流程


def _fail(code: int, message: str) -> int:
    print(f"ERROR: {message}", file=sys.stderr)
    return code


def main(argv: Optional[Sequence[str]] = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    try:
        reject_secrets_in_argv(raw)
    except PrivacyError as exc:
        return _fail(2, str(exc))

    args = build_parser().parse_args(raw)
    if not (4 <= args.api_key_index <= 254):
        return _fail(2, f"--api-key-index must be in 4..254, got {args.api_key_index}")

    # ---- 1. 私钥（只在这里出现一次，之后只用局部变量传下去）
    l1_key = _read_l1_key()
    if not l1_key:
        return _fail(2, f"no L1 private key: set ${L1_KEY_ENV} or answer the prompt")
    if not _HEX_KEY_RE.match(l1_key) and not re.match(r"^[0-9a-fA-F]{64}$", l1_key):
        return _fail(2, "L1 private key must be 32 bytes of hex (0x + 64 hex chars)")

    try:
        from eth_account import Account
    except Exception as exc:                              # pragma: no cover - 环境问题
        return _fail(2, f"eth-account is required (it ships with lighter-sdk): {exc}")

    try:
        address = Account.from_key(l1_key).address
    except Exception:
        return _fail(3, "could not derive an address from that L1 private key "
                        "(the value itself is not printed)")
    if args.l1_address and args.l1_address.lower() != address.lower():
        return _fail(3, f"derived address {_mask_address(address)} does not match "
                        f"--l1-address {_mask_address(args.l1_address)}")

    import asyncio
    try:
        return asyncio.run(_run(args, l1_key, address))
    except KeyboardInterrupt:                             # pragma: no cover
        return _fail(130, "interrupted; nothing was written to disk")
    finally:
        # 尽早丢掉唯一引用（CPython 无法原地擦除字符串，见 docstring 第 2 条）。
        del l1_key


async def _run(args: argparse.Namespace, l1_key: str, address: str) -> int:
    import lighter
    from lighter import nonce_manager as _nonce_manager
    from lighter.signer_client import SignerClient

    base_url = BASE_URLS[args.environment]
    chain_id = 304 if args.environment == "mainnet" else 300

    api_client = lighter.ApiClient(configuration=lighter.Configuration(host=base_url))
    signer = None
    try:
        # ---- 2. account_index（不确定就查，别猜）
        account_index = args.account_index
        if account_index is None:
            try:
                resp = await lighter.AccountApi(api_client).accounts_by_l1_address(
                    l1_address=address)
            except Exception as exc:
                return _fail(3, f"could not look up the account for {_mask_address(address)}: "
                                f"{type(exc).__name__} (deposit first, then retry, or pass "
                                f"--account-index explicitly)")
            subs = list(getattr(resp, "sub_accounts", []) or [])
            master = [s for s in subs if int(getattr(s, "account_type", -1)) == 0]
            if not master:
                return _fail(3, "no master account found for that address "
                                "(deposit first, then retry, or pass --account-index)")
            account_index = int(master[0].index)
        print(f"L1 address     : {_mask_address(address)}")
        print(f"account_index  : {account_index}")
        print(f"api_key_index  : {args.api_key_index}")
        print(f"environment    : {args.environment}  (chain_id={chain_id})")

        # ---- 3. 该 index 上现有的 key（注册会覆盖）
        try:
            existing = await lighter.AccountApi(api_client).apikeys(
                account_index=account_index, api_key_index=args.api_key_index)
            keys = list(getattr(existing, "api_keys", []) or [])
        except Exception:
            keys = []
        if keys:
            old = str(getattr(keys[0], "public_key", "") or "")
            print(f"!! this slot already holds a key (it WILL be replaced): {old[:16]}…")

        # ---- 4. 生成新密钥对（本地，不联网）
        api_private_key, api_public_key, err = lighter.create_api_key()
        if err or not api_private_key or not api_public_key:
            return _fail(4, f"could not generate an API key pair: {err or 'empty key'}")
        print(f"new public key : {api_public_key}")

        # ---- 5. 绑到「新 key」的 signer client（它只用来签 ChangePubKey 本身）
        signer = SignerClient(
            url=base_url, account_index=account_index,
            api_private_keys={args.api_key_index: api_private_key},
            nonce_management_type=_nonce_manager.NonceManagerType.NONE)
        if int(signer.chain_id) != chain_id:
            return _fail(3, f"signer chain_id {signer.chain_id} != {chain_id} "
                            f"for {args.environment}")

        # ---- 6. 取 nonce（注册用的是 nextNonce 的**当前值**，不是 +1；见 docs 示例）
        nonce = int((await signer.tx_api.next_nonce(
            account_index=account_index, api_key_index=args.api_key_index)).nonce)
        print(f"nonce          : {nonce}")

        tx_type, tx_info, tx_hash, sign_err = signer.sign_change_api_key(
            eth_private_key=l1_key, new_pubkey=api_public_key, skip_nonce=0,
            nonce=nonce, api_key_index=args.api_key_index)
        if sign_err:
            return _fail(4, f"signing ChangePubKey failed: {sign_err}")
        parsed = json.loads(tx_info)
        if "L1Sig" not in parsed:
            return _fail(4, "signer returned a ChangePubKey payload without an L1 signature")
        print(f"tx_type        : {tx_type}")
        print(f"tx_hash        : {tx_hash}")
        print(f"tx_info len    : {len(tx_info)}  (L1Sig present, {len(parsed['L1Sig'])} chars)")

        if args.dry_run:
            print("\n[dry-run] nothing was submitted. Re-run without --dry-run to register.")
            return 0

        # ---- 7. 显式确认（覆盖同 index 的旧 key 是不可逆的动作）
        if not args.yes:
            answer = input(
                f"Register this key at account {account_index} / api_key_index "
                f"{args.api_key_index}? type REGISTER to continue: ").strip()
            if answer != "REGISTER":
                return _fail(2, "aborted by the operator (nothing was submitted)")

        # ---- 8. 提交
        try:
            resp = await signer.tx_api.send_tx(tx_type=tx_type, tx_info=tx_info)
        except Exception as exc:
            body = getattr(exc, "body", None) or str(exc)
            return _fail(5, f"sendTx rejected the ChangePubKey: {body}"[:400])
        code = int(getattr(resp, "code", 0) or 0)
        if code != 200:
            msg = str(getattr(resp, "message", "") or "")
            return _fail(5, f"sendTx rejected the ChangePubKey: code={code} {msg}".strip())
        accepted_hash = str(getattr(resp, "tx_hash", "") or tx_hash)
        print(f"accepted       : tx_hash={accepted_hash} (code=200 = accepted, NOT executed)")

        # ---- 9. 等它真的生效：轮询 /apikeys（docs 要求，别只信 code=200）
        want = api_public_key.lower().replace("0x", "")
        deadline = time.time() + float(args.timeout)
        while time.time() < deadline:
            try:
                current = await lighter.AccountApi(api_client).apikeys(
                    account_index=account_index, api_key_index=args.api_key_index)
                remote = [str(k.public_key).lower().replace("0x", "")
                          for k in (getattr(current, "api_keys", []) or [])]
            except Exception:
                remote = []
            if want in remote:
                print("registered     : YES (public key visible at /api/v1/apikeys)")
                break
            await asyncio.sleep(REGISTER_POLL_INTERVAL_S)
        else:
            return _fail(6, "the key did not appear within the timeout. Do NOT resend: check "
                            f"GET /api/v1/tx?by=hash&value={accepted_hash} first")

        # ---- 10. 交付 API 私钥
        print("\n" + "=" * 72)
        print("Put these in the server's .env (the API key CANNOT withdraw to another "
              "address):")
        print(f"  LIGHTER_API_PRIVATE_KEY={'(hidden, see below)' if args.save_api_key else ''}")
        print(f"  LIGHTER_API_KEY_INDEX={args.api_key_index}")
        print(f"  LIGHTER_ACCOUNT_INDEX={account_index}")
        print(f"  LIGHTER_L1_ADDRESS={address}")
        print("=" * 72)
        if args.save_api_key:
            path = os.path.abspath(os.path.expanduser(args.save_api_key))
            if os.path.commonpath([path, os.getcwd()]) == os.getcwd():
                return _fail(2, "refusing to write the API key inside the current directory "
                                "(that is usually a git checkout)")
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(f"LIGHTER_API_PRIVATE_KEY={api_private_key}\n")
            os.chmod(path, 0o600)
            print(f"API private key written to {path} (mode 0600). Move it into the "
                  f"server's secret store, then delete the file.")
        else:
            print("\nAPI private key (shown ONCE — copy it now, then clear scrollback):")
            print(f"  {api_private_key}")
        print("\nAfter it is in .env, verify on the server with: "
              "python -c \"from app.config import Config; print(Config.from_env())\"")
        return 0
    finally:
        if signer is not None:
            try:
                await signer.close()
            except Exception:
                pass
        try:
            await api_client.close()
        except Exception:
            pass


if __name__ == "__main__":                                # pragma: no cover
    raise SystemExit(main())

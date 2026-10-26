"""Operations commands: python -m balebot.cli <create-admin|purge|set-webhook|poll>."""

import argparse
import asyncio

from sqlalchemy import select

from balebot.config import get_settings
from balebot.db import create_all
from balebot.gateway.updates import process_update
from balebot.main import build_state, configure_logging
from balebot.models import AdminUser
from balebot.security import generate_password, hash_password
from balebot.services.chat import purge_old_messages
from balebot.vault import Vault


async def create_admin(username: str) -> None:
    state = build_state(get_settings())
    await create_all(state.engine)
    password = generate_password()
    async with state.sessionmaker() as s:
        if await s.scalar(select(AdminUser).where(AdminUser.username == username)):
            raise SystemExit(f"admin '{username}' already exists")
        s.add(AdminUser(username=username, password_hash=hash_password(password)))
        await s.commit()
    # shown once, never stored in clear text
    print(f"username: {username}\npassword: {password}\n(shown once - change it on first login)")


async def sandbox_reap() -> None:
    state = build_state(get_settings())
    if state.sandbox is None:
        raise SystemExit("sandbox is not enabled")
    stopped, destroyed = await state.sandbox.reap()
    print(f"stopped {stopped}, destroyed {destroyed}")


async def purge() -> None:
    state = build_state(get_settings())
    async with state.sessionmaker() as s:
        n = await purge_old_messages(s, state.settings.message_retention_days)
        await s.commit()
    print(f"deleted {n} messages")


async def set_webhook() -> None:
    cfg = get_settings()
    state = build_state(cfg)
    url = f"{cfg.webhook_base_url.rstrip('/')}/bale/webhook/{cfg.webhook_secret}"
    print("webhook set:", await state.bale.set_webhook(url))


async def poll() -> None:
    """Dev mode: long-polling instead of a webhook."""
    configure_logging()
    state = build_state(get_settings())
    await create_all(state.engine)
    offset = 0
    while True:
        try:
            updates = await state.bale.get_updates(offset)
        except Exception:  # noqa: BLE001
            await asyncio.sleep(5)
            continue
        for u in updates:
            offset = u["update_id"] + 1
            await process_update(state, u)


def main() -> None:
    p = argparse.ArgumentParser(prog="balebot")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("create-admin")
    a.add_argument("--username", default="admin")
    sub.add_parser("purge")
    sub.add_parser("gen-vault-key")
    sub.add_parser("sandbox-reap")
    sub.add_parser("set-webhook")
    sub.add_parser("poll")
    args = p.parse_args()
    if args.cmd == "gen-vault-key":
        # goes into BALEBOT_VAULT_KEY; losing it makes every stored user key unreadable
        print(Vault.generate_key())
    elif args.cmd == "sandbox-reap":
        asyncio.run(sandbox_reap())
    elif args.cmd == "create-admin":
        asyncio.run(create_admin(args.username))
    elif args.cmd == "purge":
        asyncio.run(purge())
    elif args.cmd == "set-webhook":
        asyncio.run(set_webhook())
    else:
        asyncio.run(poll())


if __name__ == "__main__":
    main()

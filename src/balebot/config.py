from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Process-level settings from env. Runtime-tunable values live in the DB (admin panel)."""

    model_config = SettingsConfigDict(env_prefix="BALEBOT_", env_file=".env", extra="ignore")

    database_url: str = "sqlite+aiosqlite:///./balebot.db"
    bale_token: str = ""
    webhook_base_url: str = "https://roditeam.ir"
    webhook_secret: str = ""
    base_model_api_key: str = ""
    # urlsafe-base64 of 32 bytes (cli gen-vault-key); empty = personal keys are switched off
    vault_key: str = ""

    # panel sessions; empty secret = random per process (everyone is logged out on restart)
    session_secret: str = ""
    cookie_secure: bool = True

    default_base_url: str = "https://api.z.ai/api/paas/v4"
    default_base_model: str = "glm-4.7-flash"
    default_system_prompt: str = (
        "You are a helpful assistant in the Bale messenger. "
        "Reply in the user's language (usually Persian)."
    )
    default_daily_quota: int = 100

    # --- agent sandbox (phase 4); off until the Incus side is set up ---
    sandbox_enabled: bool = False
    incus_bin: str = "incus"
    incus_project: str = "balebot"  # restricted project: the bot can only touch its own sandboxes
    sandbox_image: str = "agent-base"  # template instance the per-user sandboxes are copied from
    sandbox_cpu: int = 1
    sandbox_memory: str = "512MiB"
    # per-sandbox root quota = measured template size + the free space each user should get.
    # Needs a pool with quotas (btrfs/zfs), not `dir`. Set after building the template.
    sandbox_disk: str = "5GiB"
    sandbox_processes: int = 256
    sandbox_idle_minutes: int = 30
    sandbox_destroy_days: int = 14
    agent_cost: int = 5  # free-quota units one /agent run costs (own-key runs are free)
    agent_timeout: int = 600
    agent_max_turns: int = 20
    agent_max_llm_calls: int = 40  # per run, enforced by the LLM proxy
    agent_max_concurrent: int = 3
    agent_toolsets: str = ""  # comma list passed to hermes -t; empty = Hermes defaults
    # secret that signs the short-lived tokens the sandbox uses to reach the LLM proxy
    proxy_secret: str = ""
    # how a sandbox reaches the proxy (bridge address of the sandbox network)
    proxy_url: str = "http://10.77.0.1:8001/v1"

    timezone: str = "Asia/Tehran"
    message_retention_days: int = 183
    history_turns: int = 10
    max_input_chars: int = 4000


@lru_cache
def get_settings() -> Settings:
    return Settings()

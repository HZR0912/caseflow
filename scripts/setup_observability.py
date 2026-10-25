"""Create local-only Langfuse credentials without printing secrets or overwriting them."""

import secrets
from pathlib import Path


def main():
    path = Path("tmp/observability.env")
    path.parent.mkdir(exist_ok=True)
    if path.exists():
        print("Existing tmp/observability.env retained.")
        return
    values = {
        "OBS_ENCRYPTION_KEY": secrets.token_hex(32),
        "OBS_SALT": secrets.token_hex(32),
        "OBS_AUTH_SECRET": secrets.token_hex(32),
        "OBS_USER_PASSWORD": secrets.token_urlsafe(24),
        "LANGFUSE_PUBLIC_KEY": "pk-lf-" + secrets.token_hex(16),
        "LANGFUSE_SECRET_KEY": "sk-lf-" + secrets.token_hex(24),
        "LANGFUSE_BASE_URL": "http://127.0.0.1:13000",
    }
    path.write_text("".join(f"{k}={v}\n" for k, v in values.items()), encoding="utf-8")
    print("Created tmp/observability.env (ignored by Git). Local login: local@example.com.")


if __name__ == "__main__":
    main()

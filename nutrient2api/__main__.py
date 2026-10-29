import sys

from .server import Config, serve
from .storage import Store

if __name__ == "__main__":
    if len(sys.argv) == 1:
        serve()
    elif len(sys.argv) == 2 and sys.argv[1] in (
        "reset-admin", "setup-code", "key-import-token", "rotate-key-import-token", "client-token"
    ):
        cfg = Config()
        store = Store(cfg.data_dir, cfg.api_key, cfg.client_token)
        command = sys.argv[1]
        if command == "key-import-token":
            print(store.setting("key_import_token"))
        elif command == "rotate-key-import-token":
            print(store.rotate_key_import_token())
        elif command == "client-token":
            print(store.setting("client_token"))
        else:
            code = store.reset_admin() if command == "reset-admin" else store.setting("setup_code")
            print("INITIAL SETUP CODE: " + code if code else "Admin already initialized. Use reset-admin only to reset the administrator password.")
    else:
        raise SystemExit("Usage: python -m nutrient2api [setup-code|reset-admin|key-import-token|rotate-key-import-token|client-token]")

import sys

from .server import Config, serve
from .storage import Store

if __name__ == "__main__":
    if len(sys.argv) == 1:
        serve()
    elif sys.argv[1:] in (["reset-admin"], ["setup-code"]):
        cfg = Config()
        store = Store(cfg.data_dir, cfg.api_key, cfg.client_token)
        code = store.reset_admin() if sys.argv[1] == "reset-admin" else store.setting("setup_code")
        print("INITIAL SETUP CODE: " + code if code else "Admin already initialized. Use reset-admin only to reset the administrator password.")
    else:
        raise SystemExit("Usage: python -m nutrient2api [setup-code|reset-admin]")

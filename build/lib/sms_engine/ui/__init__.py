"""Human-review dashboard (server-rendered Jinja2). Intentionally minimal —
the UI exists to make the engineering legible, not to be the product.
"""

from sms_engine.ui.routes import mount_ui

__all__ = ["mount_ui"]

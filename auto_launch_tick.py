"""Opt-in scheduler-facing launch tick, deliberately disabled until wired.

No network access or Amazon mutation occurs when disabled. The production
scheduler must never supply a live flag as a substitute for server opt-in.
"""
import os
from auto_launch_coordinator import LaunchBlocked

def launch_tick(*, coordinator_factory=None, inputs_factory=None, pipeline_factory=None):
    if os.getenv("AUTO_LAUNCH_ENABLED", "false").lower() != "true":
        return {"status": "disabled", "created": 0}
    if pipeline_factory is None or inputs_factory is None:
        raise LaunchBlocked("Production launch adapters not configured")
    args = inputs_factory()
    if not isinstance(args, dict):
        raise LaunchBlocked("Missing verified launch inputs")
    pipeline = pipeline_factory()
    if not getattr(pipeline, "enable", False):
        raise LaunchBlocked("Coordinator not explicitly enabled")
    return pipeline.run(**args)


class ProductionPipeline:
    enable = True

    def run(self, **inputs):
        from optimize_campaigns import AmazonAdsClient
        from automation_store import GCSState
        from auto_launch_production import ProductionLaunchAmazon
        from auto_launch_guard import ProductionLaunchGuard
        from auto_launch_provisioning import PausedProvisioner
        from auto_launch_coordinator import AutoLaunchCoordinator
        from auto_launch_pipeline import run_pipeline
        client = AmazonAdsClient()
        store = GCSState('auto-launch')
        adapter, guard = ProductionLaunchAmazon(client), ProductionLaunchGuard(client)
        coordinator = AutoLaunchCoordinator(store=store, amazon=adapter, guard=guard, enable=True)
        return run_pipeline(coordinator=coordinator, provisioner=PausedProvisioner(store=store, adapter=adapter),
                            store=store, adapter=adapter, guard=guard, **inputs)


def production_inputs():
    from automation_store import GCSState
    from optimize_campaigns import load_products, normalized_product
    data = GCSState('profit-data').read()
    # Selection's availability alias is derived only from verified inventory.
    verified = {a: {**p, 'available': p.get('inventory')} for a, p in data.get('products', {}).items()}
    products = [normalized_product(row) for row in load_products()]
    return {'products': [p for p in products if p.get('active')], 'verified': verified,
            'policy': GCSState('profit-policy').read().get('policy'),
            'account_profile': os.getenv('AMAZON_ADS_PROFILE_ID') or os.getenv('AMAZON_PROFILE_ID')}

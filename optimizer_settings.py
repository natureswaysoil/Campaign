"""Effective production settings. Offline optimizer_config.json is not live policy."""
import os
from pydantic import BaseModel, ConfigDict, Field, model_validator

class BidSettings(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    minimum: float = Field(default=.10, gt=0)
    maximum: float = Field(default=2.50, gt=0)
    protect: float = Field(default=.35, gt=0, le=1)
    taper: float = Field(default=.45, gt=0, le=1)
    prime: float = Field(default=1.15, gt=0, le=2)
    acos_ceiling: float = Field(default=.38, gt=0, le=1)
    min_spend: float = Field(default=20, ge=0)
    reduce: float = Field(default=.75, gt=0, le=1)
    severe: float = Field(default=.50, gt=0, le=1)
    zero_sales: float = Field(default=.35, gt=0, le=1)
    prime_position: float = Field(default=.85, ge=0, le=1)
    normal_position: float = Field(default=.55, ge=0, le=1)
    prime_start: int = Field(default=10, ge=0, le=23)
    prime_end: int = Field(default=20, ge=0, le=23)

    @model_validator(mode='after')
    def ranges(self):
        if self.minimum > self.maximum or self.prime_start > self.prime_end:
            raise ValueError('Invalid bid bounds or prime window')
        return self

ENV_FIELDS = {'minimum': 'MIN_DAYPART_BID', 'maximum': 'MAX_DAYPART_BID',
    'protect': 'PROTECT_BID_MULTIPLIER', 'taper': 'TAPER_BID_MULTIPLIER',
    'prime': 'PRIME_BID_MULTIPLIER', 'acos_ceiling': 'ACOS_CIRCUIT_BREAKER_CEILING',
    'min_spend': 'ACOS_CIRCUIT_BREAKER_MIN_SPEND', 'reduce': 'ACOS_REDUCE_MULTIPLIER',
    'severe': 'ACOS_SEVERE_MULTIPLIER', 'zero_sales': 'ACOS_ZERO_SALES_MULTIPLIER',
    'prime_position': 'PRIME_BID_POSITION', 'normal_position': 'NORMAL_BID_POSITION',
    'prime_start': 'PRIME_TIME_START', 'prime_end': 'PRIME_TIME_END'}

def bid_settings():
    return BidSettings(**{field: os.environ[name] for field, name in ENV_FIELDS.items() if name in os.environ})

def effective_settings(policy):
    return {'profit_policy': policy, 'profit_policy_source': 'saved Profit dashboard policy',
            'daypart': bid_settings().model_dump(), 'daypart_source': 'validated runtime environment or defaults',
            'daypart_timezone': 'America/New_York',
            'report_timezone': (policy or {}).get('timezone'),
            'winner_acos': {'runtime_limit': float(os.getenv('WINNER_MAX_ACOS', '.35')),
                            'dashboard_limit': (policy or {}).get('target_acos'),
                            'rule': 'Lower of runtime winner threshold and saved dashboard target'},
            'offline_config': 'optimizer_config.json applies to the offline planner only',
            'precedence': 'Profit guard caps every proposed live bid after dayparting and circuit breakers'}

def winner_acos_limit(runtime_limit):
    """Saved dashboard policy can tighten winner selection in durable production."""
    if not os.getenv('AUTOMATION_STATE_BUCKET'):
        return runtime_limit  # Offline planner has no saved account policy.
    from automation_store import GCSState
    from profit_intelligence import Policy
    raw = GCSState('profit-policy').read().get('policy')
    if not raw:
        raise ValueError('Profit policy required for production winner selection')
    return min(runtime_limit, Policy.model_validate(raw).target_acos)

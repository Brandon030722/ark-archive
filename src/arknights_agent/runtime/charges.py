"""Provider usage estimates, distinct from reservations and provider invoices.

These are the verified DeepSeek-V4.1-Flash CNY rates. Unknown holiday status
inside a weekday peak window yields a range; never guess an invoice amount.
"""
from datetime import timedelta, timezone
from decimal import Decimal

FIELDS = ('prompt_tokens', 'completion_tokens', 'total_tokens',
          'prompt_cache_hit_tokens', 'prompt_cache_miss_tokens')
BEIJING = timezone(timedelta(hours=8))
RATES = {'offpeak': (Decimal('.02'), Decimal('1'), Decimal('4')),
         'peak': (Decimal('.04'), Decimal('2'), Decimal('8'))}


def validated_usage(usage):
    if (not isinstance(usage, dict)
        or any(type(usage.get(k)) is not int or usage[k] < 0 for k in FIELDS)
        or usage['prompt_tokens'] != usage['prompt_cache_hit_tokens'] + usage['prompt_cache_miss_tokens']
        or usage['total_tokens'] != usage['prompt_tokens'] + usage['completion_tokens']):
        raise ValueError('invalid_provider_usage')
    return {k: usage[k] for k in FIELDS}


def tariff_window(stamp, *, statutory_holiday=None):
    if stamp.tzinfo is None or statutory_holiday not in (None, True, False):
        raise ValueError('timezone_or_holiday_invalid')
    local = stamp.astimezone(BEIJING)
    if local.weekday() >= 5 or statutory_holiday is True or not (9 <= local.hour < 12 or 14 <= local.hour < 18):
        return 'offpeak'
    return 'peak' if statutory_holiday is False else 'holiday_unverified'


def estimate_charge(usage, *, started_at=None, ended_at=None, statutory_holiday=None):
    counts = validated_usage(usage)
    amounts = {name: (counts['prompt_cache_hit_tokens'] * hit
                      + counts['prompt_cache_miss_tokens'] * miss
                      + counts['completion_tokens'] * output) / 1_000_000
               for name, (hit, miss, output) in RATES.items()}
    window = 'time_unverified'
    if started_at is not None and ended_at is not None:
        if started_at.tzinfo is None or ended_at.tzinfo is None or ended_at < started_at:
            raise ValueError('request_interval_invalid')
        # Examine the complete interval, including hourly tariff boundaries.
        cursor = started_at
        windows = {tariff_window(ended_at, statutory_holiday=statutory_holiday)}
        while cursor <= ended_at:
            windows.add(tariff_window(cursor, statutory_holiday=statutory_holiday))
            cursor = cursor.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        window = next(iter(windows)) if len(windows) == 1 else 'tariff_boundary_crossed'
    exact = amounts.get(window)
    return {'usage': counts, 'tariff_window': window,
            'estimated_charge_cny': str(exact) if exact is not None else None,
            'estimated_min_cny': str(exact if exact is not None else amounts['offpeak']),
            'estimated_max_cny': str(exact if exact is not None else amounts['peak']),
            'is_provider_invoice': False, 'safety_margin_applied': False,
            'rates_source': 'https://api-docs.deepseek.com/zh-cn/quick_start/pricing/'}

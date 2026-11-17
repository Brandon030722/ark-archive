"""Small shared privacy boundary: never log provider bodies or credentials."""

import re

# Header and environment aliases use the same terminal credential names:
# x-api-key, ARK_DEEPSEEK_API_KEY, openai_api_key, and similar prefixed keys.
_KEY = (
    r"(?:[a-z0-9]+[_-])*(?:api[_-]?key|(?:access|refresh|id)[_-]?token|"
    r"token|authorization|password|secret|client[_-]?secret)"
)
_SENSITIVE_KEY = re.compile(_KEY, re.I)
# Consume quoted values as a unit, including whitespace and escaped quotes.
# Consume an Authorization Bearer value before the generic unquoted value;
# otherwise that branch would remove only 'Bearer' and leave its credential.
_VALUE = (
    r'"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|'
    r"Bearer[ \t]+[^\s,;}\"']+|[^\s,;}\"']+"
)
_SECRETS = re.compile(
    rf"\b{_KEY}[\"']?\s*[:=]\s*(?:{_VALUE})|"
    r"\bsk-[A-Za-z0-9_-]{16,}|\bBearer[ \t]+[^\s,;}\"']+|"
    r"\b[a-z][a-z0-9+.-]*://[^\s/@]+@[^\s]+",
    re.I,
)

# Client descriptions use {blackboard_variable:0%} display instructions. The
# format suffix is not a credential value, even if a variable ends in _token.
# Recognize only this complete, unquoted numeric-format syntax. Actual
# credential names, dictionary keys and every surrounding string remain under
# the original scanner. The returned source text/hash is never rewritten.
_DISPLAY_FORMAT = re.compile(r"\{([A-Za-z_][A-Za-z0-9_.]*):0(?:\.0+)?%?\}")
_CREDENTIAL_FORMAT_NAME = re.compile(
    r"(?:[a-z0-9]+[_-])*(?:api[_-]?key|(?:access|refresh|id)[_-]?token|"
    r"authorization|password|secret|client[_-]?secret)|token", re.I,
)
_CREDENTIAL_FORMAT_WORD = re.compile(
    r"(?:^|_)(?:api|access|refresh|authorization|auth|password|secret|client)(?:_|$)", re.I,
)


def _credential_scan_text(value):
    def display(match):
        credential = any(_CREDENTIAL_FORMAT_NAME.fullmatch(part)
                         or _CREDENTIAL_FORMAT_WORD.search(part) for part in match[1].split("."))
        return match.group() if credential else "{numeric_display_format}"

    return _DISPLAY_FORMAT.sub(display, value)


def contains_secret(value):
    if isinstance(value, dict):
        return any(
            _SENSITIVE_KEY.fullmatch(str(key))
            or contains_secret(item) for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(contains_secret(item) for item in value)
    return bool(_SECRETS.search(_credential_scan_text(str(value))))


def redact(value):
    if isinstance(value, str):
        # Mask the complete sensitive string. Partial substitution can retain
        # credentials after an unfamiliar Authorization scheme or spaces.
        return "[REDACTED]" if contains_secret(value) else value
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item) for item in value)
    if isinstance(value, dict):
        return {
            key: "[REDACTED]"
            if _SENSITIVE_KEY.fullmatch(str(key))
            else redact(item)
            for key, item in value.items()
        }
    return value

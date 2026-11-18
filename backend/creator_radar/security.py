import re
from urllib.parse import urlsplit, urlunsplit

SENSITIVE = (
    r"authorization|proxy[-_]?authorization|set[-_]?cookie|cookies?|"
    r"[\w-]*(?:api[-_]?key|token|password|secret|signature|credential)|sig|session[-_]?id"
)
# Header values can contain multiple credentials separated by spaces and semicolons.
HEADER_LINE = re.compile(
    r"(?im)\b(authorization|proxy[-_]?authorization|set[-_]?cookie|cookies?)\s*[:=][^\r\n]*"
)
QUOTED = re.compile(
    rf"""(?i)(["']?(?:{SENSITIVE})["']?\s*[:=]\s*)(["'])(?:\\.|(?!\2)[^\\\r\n])*\2"""
)
UNQUOTED = re.compile(rf"(?i)(\b(?:{SENSITIVE})\b\s*[:=]\s*)(?!\[已脱敏\])[^\s,;&}}\]]+")
BEARER = re.compile(r"(?i)\b(?:bearer|basic)\s+[a-z0-9+/_.=-]+")
URL = re.compile(r"""(?i)https?://[^\s<>"']+""")


def _public_url(match: re.Match) -> str:
    try:
        parsed = urlsplit(match[0])
        return urlunsplit((parsed.scheme, parsed.netloc.rsplit("@", 1)[-1], parsed.path, "", ""))
    except ValueError:
        return "[链接已脱敏]"


def redact(value: str) -> str:
    value = URL.sub(_public_url, value)
    value = QUOTED.sub(r"\1[已脱敏]", value)
    value = HEADER_LINE.sub(r"\1: [已脱敏]", value)
    value = BEARER.sub("[已脱敏]", value)
    return UNQUOTED.sub(r"\1[已脱敏]", value)


def safe_result(value):
    if isinstance(value, dict):
        return {
            key: "[已脱敏]" if re.fullmatch(SENSITIVE, key, re.I) else safe_result(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [safe_result(item) for item in value]
    return redact(value) if isinstance(value, str) else value

import re
from typing import Dict, Tuple


_INDIRECT_INJECTION = re.compile(
    r"(ignore\s+(all\s+)?previous\s+instructions|"
    r"system\s+prompt|developer\s+message|"
    r"reveal\s+.*(secret|password|token)|"
    r"do\s+not\s+follow\s+the\s+user)",
    re.IGNORECASE,
)


def guard_retrieved_content(text: str) -> Tuple[str, Dict[str, object]]:
    raw = str(text or "")
    suspicious = bool(_INDIRECT_INJECTION.search(raw))
    # Retrieved content is always explicitly data-delimited. Suspicious content
    # remains citeable but cannot masquerade as system/tool instructions.
    wrapped = (
        "<retrieved_document trust="untrusted-data">\n"
        + raw
        + "\n</retrieved_document>"
    )
    return wrapped, {"indirectPromptInjection": suspicious}

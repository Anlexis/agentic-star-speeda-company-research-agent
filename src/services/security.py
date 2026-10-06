"""Input screening and bounds for CMN-C2-287 (caller-facing contract).

Pure, stateless domain helpers (NOT framework gate methods). Everything a
caller can influence passes through this module before it reaches the pipeline:

* :func:`sanitize_query` strips markup and caps length;
* :func:`screen_injection` refuses prompt-injection payloads - run it on the
  RAW text as well as on the sanitized text, because the markup strip both
  removes control tokens and re-assembles directives that were split by tags;
* :func:`validate_context_identifier` locks every caller value that can reach
  the outbound request or the rendered answer to a short inert identifier;
* :func:`finite_in_range` parses a configured number fail-CLOSED, so a
  non-finite or out-of-range value raises instead of silently comparing False.

Two helpers serve the OUTBOUND side of the same contract:

* :func:`walk_strings` renders every string reachable in a response - mapping
  KEYS included, because a key is text on the external surface exactly as much
  as a value, and a value-only walk is blind to it;
* :func:`withhold_credentials` removes a credential-shaped fragment from a
  label that is about to be written somewhere. Both this module's pattern and
  the framework's detector apply; the framework's is the floor.

None of these helpers ever echo a rejected value: they name the field.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterator
from typing import Any

from framework.security.credential_detector import detect_credentials

_HTML_TAG_RE = re.compile(r"<[^>]+>")

# Credential-shaped strings. Kept alongside - not instead of - the framework's
# detector: this one catches shapes the framework does not, the framework's
# catches shapes this one does not, and the union is the floor.
CREDENTIAL_LIKE_RE = re.compile(r"eyJ[A-Za-z0-9._-]{10,}|sk-[A-Za-z0-9]{20,}|Bearer\s+[A-Za-z0-9._-]{16,}")

# Stand-in for a fragment that cannot itself be written into a label. A label
# built from caller-supplied text (a mapping key, a rejected field name) travels
# in error_log, and the framework's S-3 scan RAISES on a credential pattern
# anywhere in a node result - which returns a bare error with no
# `formatted_output` key at all, re-opening the `formatted_output or result`
# projection that the containment exists to close.
WITHHELD = "<withheld>"

DEFAULT_MAX_LENGTH = 4000

# Caller identifiers (company code / ticker / hint). Deliberately inert: a short
# alphanumeric run with `_` and `-`, nothing that can carry markup, whitespace,
# a directive, or a credential-shaped blob. Everything rendered back to the
# caller or forwarded to the external request is drawn from this alphabet.
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,19}$")
IDENTIFIER_MAX_LENGTH = 20

# Bounds on the caller context channel as a whole.
MAX_CONTEXT_KEYS = 8
MAX_CONTEXT_VALUE_CHARS = 64

# Chat-template control tokens are their own attack class: they are not
# directive PHRASES, so a phrase-based screen misses them entirely, and the
# markup strip silently deletes `<|...|>` (it looks like a tag) while forwarding
# whatever followed it as ordinary text. Screening the raw text catches the
# token; screening the sanitized text catches a directive that was split by
# markup ("ig<b>nore all previous instructions") and re-joined by the strip.
_CONTROL_TOKEN_RE = re.compile(
    r"<\|[^\n|]{0,64}\|>"  # <|im_start|>, <|endoftext|>, <|system|>
    r"|\[/?INST\]"  # [INST] / [/INST]
    r"|<</?SYS>>",  # <<SYS>> / <</SYS>>
    re.IGNORECASE,
)

# Directive forms. Anchored at a statement boundary and requiring an explicit
# instruction object, so ordinary research wording is untouched: "show the
# company overview", "show me the rules for industry classification" and
# "look up the company that files disregard notices" must all pass.
_DIRECTIVE_RE = re.compile(
    r"(?:\A|[.!?;\n]\s*)(?:please\s+)?"
    r"(?:ignore|disregard|forget|override|bypass)\s+"
    r"(?:all\s+|any\s+|the\s+|your\s+)*"
    r"(?:previous|prior|earlier|above|preceding|system|these|those)\s+"
    r"(?:instructions?|rules?|prompts?|directives?|constraints?)",
    re.IGNORECASE,
)
_ROLE_HIJACK_RE = re.compile(
    r"(?:\A|[.!?;\n]\s*)you\s+are\s+(?:now|no\s+longer)\b",
    re.IGNORECASE,
)
_PROMPT_EXFIL_RE = re.compile(
    r"(?:reveal|print|show|repeat|output|display|dump)\s+(?:me\s+)?"
    r"(?:your\s+(?:system\s+)?(?:prompt|instructions?)|the\s+system\s+prompt)",
    re.IGNORECASE,
)

_SCREENS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("control_token", _CONTROL_TOKEN_RE),
    ("instruction_override", _DIRECTIVE_RE),
    ("role_hijack", _ROLE_HIJACK_RE),
    ("prompt_exfiltration", _PROMPT_EXFIL_RE),
)


class ContextValidationError(ValueError):
    """A caller-supplied context value failed its bounds check.

    The message names the offending FIELD and the expected shape. It never
    contains the rejected value - an error log is an output surface too.
    """


def sanitize_query(query: str, max_length: int = DEFAULT_MAX_LENGTH) -> str:
    """Strip markup tags and cap length.

    Sanitizing is not refusal: this only removes markup. Call
    :func:`screen_injection` on both the raw and the sanitized text.
    """
    cleaned = _HTML_TAG_RE.sub("", query)
    return cleaned[:max_length]


def screen_injection(*texts: str) -> str | None:
    """Return the violation class of the first screened form found, else None.

    Pass every representation of the caller's text - the raw string AND the
    sanitized string - because the two hide different attacks from each other.
    """
    for text in texts:
        if not text:
            continue
        for label, pattern in _SCREENS:
            if pattern.search(text):
                return label
    return None


def walk_strings(value: Any, path: str = "") -> Iterator[tuple[str, str]]:
    """Yield ``(path, text)`` for every string reachable in *value*.

    Mapping KEYS are yielded as strings in their own right. A key is on the
    external surface exactly as much as a value is, and a scan that reads only
    values reports zero findings on a payload whose key carries the secret.
    Nested mappings and sequences are walked: caller or third-party text one
    level down is no less published than a top-level string.
    """
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, item in value.items():
            key_text = key if isinstance(key, str) else str(key)
            child = f"{path}['{key_text}']"
            yield f"{child} (key)", key_text
            yield from walk_strings(item, child)
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            yield from walk_strings(item, f"{path}[{index}]")


def withhold_credentials(label: str) -> str:
    """Return *label* with every credential-shaped fragment replaced.

    Applied to any label built from text this template did not choose - a
    mapping key on the way into a violation entry, a rejected field name on the
    way into a refusal notice. Both this module's pattern and the framework's
    detector run: the framework's is the one that would raise on the node
    result, so it has to be satisfied, and the local one catches shapes it
    does not.
    """
    cleaned = CREDENTIAL_LIKE_RE.sub(WITHHELD, label)
    findings = detect_credentials(cleaned)
    while findings:
        first = findings[0]
        cleaned = cleaned[: first["start"]] + WITHHELD + cleaned[first["end"] :]
        findings = detect_credentials(cleaned)
    return cleaned


def mask_field_name(name: Any) -> str:
    """Render an unrecognised context key safely for an error message.

    A rejected key is caller-controlled text that would otherwise be echoed
    into an error log verbatim. Recognisable keys are short and inert; anything
    else is reported by shape, never by content.

    "Inert" is not "harmless": an AWS access key id is twenty characters of
    ``[A-Z0-9]`` and matches IDENTIFIER_RE exactly, so the pass-through branch
    is run through :func:`withhold_credentials` before it is returned.
    """
    text = name if isinstance(name, str) else type(name).__name__
    if IDENTIFIER_RE.match(text):
        return withhold_credentials(text)
    return f"<unrecognised field, {len(text)} chars>"


def validate_context_identifier(field: str, value: Any) -> str:
    """Return *value* as an inert identifier, or raise ContextValidationError.

    Fail CLOSED: a value that is not a string, is over-long, or carries
    anything outside the identifier alphabet is refused. The caller learns
    which field was wrong and what shape is expected - never what it sent.
    """
    if not isinstance(value, str):
        raise ContextValidationError(
            f"input_context['{field}'] must be a string identifier "
            f"matching [A-Za-z0-9][A-Za-z0-9_-]{{0,{IDENTIFIER_MAX_LENGTH - 1}}}"
        )
    if len(value) > MAX_CONTEXT_VALUE_CHARS:
        raise ContextValidationError(f"input_context['{field}'] exceeds {MAX_CONTEXT_VALUE_CHARS} characters")
    candidate = value.strip()
    if not candidate:
        return ""
    if not IDENTIFIER_RE.match(candidate):
        raise ContextValidationError(
            f"input_context['{field}'] must match " f"[A-Za-z0-9][A-Za-z0-9_-]{{0,{IDENTIFIER_MAX_LENGTH - 1}}}"
        )
    return candidate


def finite_in_range(
    field: str,
    value: Any,
    *,
    minimum: float,
    maximum: float,
) -> float:
    """Parse a number fail-CLOSED: finite, in range, not a bool.

    ``float("nan")`` and ``float("inf")`` parse without error and then compare
    False against every bound, so an unchecked non-finite value silently
    disables the very limit it configures. JSON delivers bare ``NaN`` /
    ``Infinity`` too, so this is reachable from data, not only from code.
    """
    if isinstance(value, bool):
        raise ContextValidationError(f"{field} must be a number, not a boolean")
    if not isinstance(value, (int, float, str)):
        raise ContextValidationError(f"{field} must be a number")
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        raise ContextValidationError(f"{field} must be a number") from None
    if not math.isfinite(parsed):
        raise ContextValidationError(f"{field} must be a finite number")
    if not (minimum <= parsed <= maximum):
        raise ContextValidationError(f"{field} must be between {minimum} and {maximum}")
    return parsed

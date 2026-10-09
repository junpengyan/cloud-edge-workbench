from __future__ import annotations

from typing import Any


DEFAULT_PASSWORD_POLICY: dict[str, Any] = {
    "minimumLength": 8,
    "maximumLength": 72,
    "minimumUppercaseLetters": 0,
    "minimumLowercaseLetters": 1,
    "minimumDigits": 0,
    "minimumSpecialCharacters": 1,
    "allowWhitespaces": False,
}


def normalize_password_policy(policy: dict[str, Any] | None) -> dict[str, Any]:
    source = policy or DEFAULT_PASSWORD_POLICY
    return {
        "minimumLength": _as_int(source.get("minimumLength"), 0),
        "maximumLength": _as_int(source.get("maximumLength"), 0),
        "minimumUppercaseLetters": _as_int(source.get("minimumUppercaseLetters"), 0),
        "minimumLowercaseLetters": _as_int(source.get("minimumLowercaseLetters"), 0),
        "minimumDigits": _as_int(source.get("minimumDigits"), 0),
        "minimumSpecialCharacters": _as_int(source.get("minimumSpecialCharacters"), 0),
        "allowWhitespaces": bool(source.get("allowWhitespaces", False)),
    }


def validate_password(password: str, policy: dict[str, Any] | None) -> list[str]:
    normalized = normalize_password_policy(policy)
    errors: list[str] = []
    if len(password) < normalized["minimumLength"]:
        errors.append(f"至少 {normalized['minimumLength']} 位")
    if normalized["maximumLength"] and len(password) > normalized["maximumLength"]:
        errors.append(f"最多 {normalized['maximumLength']} 位")
    _append_minimum_error(errors, password, normalized["minimumUppercaseLetters"], str.isupper, "大写字母")
    _append_minimum_error(errors, password, normalized["minimumLowercaseLetters"], str.islower, "小写字母")
    _append_minimum_error(errors, password, normalized["minimumDigits"], str.isdigit, "数字")
    _append_minimum_error(errors, password, normalized["minimumSpecialCharacters"], _is_special, "特殊字符")
    if not normalized["allowWhitespaces"] and any(char.isspace() for char in password):
        errors.append("不能包含空格")
    return errors


def describe_password_policy(policy: dict[str, Any] | None) -> str:
    normalized = normalize_password_policy(policy)
    rules = [f"至少 {normalized['minimumLength']} 位"]
    if normalized["maximumLength"]:
        rules.append(f"最多 {normalized['maximumLength']} 位")
    for key, label in [
        ("minimumUppercaseLetters", "大写字母"),
        ("minimumLowercaseLetters", "小写字母"),
        ("minimumDigits", "数字"),
        ("minimumSpecialCharacters", "特殊字符"),
    ]:
        if normalized[key]:
            rules.append(f"至少 {normalized[key]} 个{label}")
    rules.append("允许空格" if normalized["allowWhitespaces"] else "不能包含空格")
    return "，".join(rules) + "。"


def _as_int(value: object, default: int) -> int:
    return int(value) if isinstance(value, int | float) else default


def _append_minimum_error(
    errors: list[str],
    password: str,
    minimum: int,
    predicate,
    label: str,
) -> None:
    if minimum and sum(1 for char in password if predicate(char)) < minimum:
        errors.append(f"至少 {minimum} 个{label}")


def _is_special(char: str) -> bool:
    return not char.isalnum() and not char.isspace()

import hmac


def is_authorized(provided_token: str | None, expected_token: str) -> bool:
    if not provided_token:
        return False
    return hmac.compare_digest(provided_token, expected_token)


def ip_allowed(client_ip: str, allowed_ips: tuple[str, ...]) -> bool:
    if not allowed_ips:
        return True
    return client_ip in allowed_ips

"""Echo command."""


def echo_command(args: str, context: dict) -> str:
    if not args:
        return "Echo what?"
    if len(args) > 200:
        args = args[:197] + "..."
    return f"Echo: {args}"

"""CLI transport validation; does not interpret business parameter semantics.

Synced into windows-bridge/argument_contract.py for standalone delivery.
"""

RESERVED_ARGUMENTS = frozenset({"--benchmark", "--output", "--output-dir", "--progress-file"})


def output_directory_value(arguments, flag):
    values = []
    for index, item in enumerate(arguments):
        if item == flag:
            values.append(arguments[index + 1] if index + 1 < len(arguments) else "")
        elif item.startswith(flag + "="):
            values.append(item[len(flag) + 1:])
    if len(values) != 1 or not values[0].strip() or values[0].startswith("--"):
        raise ValueError("A single output directory argument is required")
    return values[0]


def validate_arguments(arguments, *, mode="allowlist", allowed=(), switches=(), reserved=(), output_directory_argument=None):
    if mode not in {"allowlist", "passthrough"}:
        raise ValueError("Unsupported argument mode")
    if (
        not isinstance(arguments, list)
        or len(arguments) > 100
        or any(
            not isinstance(value, str) or "\0" in value or len(value) > 4096 for value in arguments
        )
    ):
        raise ValueError("Invalid argument list")
    if output_directory_argument:
        output_directory_value(arguments, output_directory_argument)
    if mode == "passthrough":
        # A literal option token (including --option=value and long-option
        # abbreviations) must not override a platform-managed CLI argument.
        protected = RESERVED_ARGUMENTS | set(reserved)
        for value in arguments:
            option = value.partition("=")[0]
            if option in protected or (
                option.startswith("--") and option != output_directory_argument
                and any(name.startswith(option) for name in protected)
            ):
                raise ValueError("Platform-managed arguments cannot be overridden")
        return arguments

    index, seen = 0, set()
    allowed, switches = set(allowed), set(switches)
    while index < len(arguments):
        flag = arguments[index]
        if flag in seen or flag not in allowed | switches:
            raise ValueError("Argument is not registered or is duplicated")
        seen.add(flag)
        index += 1
        if flag in allowed:
            if index >= len(arguments):
                raise ValueError("Argument value is missing")
            index += 1
    return arguments

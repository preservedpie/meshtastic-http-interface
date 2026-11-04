"""Dice command — roll NdX[+M]."""

import random
import re


def dice_command(args: str, context: dict) -> str:
    if not args:
        return f"Roll d6: {random.randint(1, 6)}"

    args = args.strip().lower()
    match = re.match(r'^(\d*)d(\d+)([+-]\d+)?$', args)
    if not match:
        return "Format: NdX (e.g., 2d6, d20, 3d8+2)"

    num_dice = int(match.group(1)) if match.group(1) else 1
    die_sides = int(match.group(2))
    modifier = int(match.group(3)) if match.group(3) else 0

    if num_dice > 20:
        return "Max 20 dice"
    if die_sides > 100:
        return "Max d100"
    if die_sides < 2:
        return "Min d2"

    rolls = [random.randint(1, die_sides) for _ in range(num_dice)]
    total = sum(rolls) + modifier

    if num_dice == 1 and modifier == 0:
        return f"Roll d{die_sides}: {rolls[0]}"

    rolls_str = "+".join(str(r) for r in rolls)
    if modifier > 0:
        return f"Roll {args}: [{rolls_str}]+{modifier} = {total}"
    if modifier < 0:
        return f"Roll {args}: [{rolls_str}]{modifier} = {total}"
    return f"Roll {args}: [{rolls_str}] = {total}"

def plural(number: int, one: str, few: str, many: str) -> str:
    """The Russian word form for a whole number: 1 поставщика, 2 поставщиков, 5 поставщиков."""
    rest = abs(number) % 100
    if 11 <= rest <= 14:
        return many
    last = rest % 10
    return one if last == 1 else few if 2 <= last <= 4 else many


def days_text(days: int | float) -> str:
    whole = int(days)
    word = "дня" if days != whole else plural(whole, "день", "дня", "дней")
    number = f"{days:g}".replace(".", ",")
    return f"{number} {word}"

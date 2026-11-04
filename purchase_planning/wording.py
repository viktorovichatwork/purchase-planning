def days_text(days: int | float) -> str:
    whole = int(days)
    last = abs(whole) % 10
    if days != whole:
        word = "дня"
    elif 11 <= abs(whole) % 100 <= 14:
        word = "дней"
    else:
        word = "день" if last == 1 else "дня" if 2 <= last <= 4 else "дней"
    number = f"{days:g}".replace(".", ",")
    return f"{number} {word}"

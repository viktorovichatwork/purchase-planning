from hashlib import sha256
from pathlib import Path

SPEC = Path(__file__).parents[1] / "docs" / "spec" / "original-task.md"
SPEC_SHA256 = "b8443ed1d74c00494acbc5d7949ce265c854621bd9e51be7afd7f925a47952e3"


def test_original_task_is_unchanged() -> None:
    text = SPEC.read_text(encoding="utf-8").replace("\r\n", "\n")
    assert sha256(text.encode("utf-8")).hexdigest() == SPEC_SHA256, (
        "docs/spec/original-task.md — исходное ТЗ заказчика, его не правят. "
        "Отступление от ТЗ оформляется ADR в docs/adr/. "
        "Если заказчик сам прислал новую версию ТЗ, замени файл и обнови SPEC_SHA256."
    )

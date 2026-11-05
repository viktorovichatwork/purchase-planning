## Проект

- Требования заказчика: `docs/spec/original-task.md` (не правится). Отступление от ТЗ — через ADR в `docs/adr/`.
- Правила ревью: `CODING_STANDARDS.md`.
- При публикации и передаче результата: `docs/agents/delivery.md`. При пересмотре процесса: `docs/retrospective.md`.
- Корень репозитория — эталонная обезличенная выгрузка (на ней CI сверяет бэктест). `inbox/` — место загрузки новой выгрузки для Pages.
- Работа — в отдельной ветке, не в `main`. Коммит, push и PR — только после ревью и явного подтверждения пользователя.
- Пограничный случай, который не меняет правило из ADR и ожидаемый результат тикета, решай сам: консервативно, без искажения данных, с флагом у SKU, решение — комментарием в issue. Спрашивай, только если решение меняет ADR или критерии приёмки.
- Бюджет токенов и времени ограничен: крупные тикеты, без повторных кругов по уже принятым решениям (они в `docs/adr/` и `GLOSSARY.md`).

## Agent skills

### Issue tracker

Issues are tracked in GitHub Issues for viktorovichatwork/purchase-planning (via `gh`). See `docs/agents/issue-tracker.md`.

### Triage labels

Default vocabulary: needs-triage, needs-info, ready-for-agent, ready-for-human, wontfix. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `GLOSSARY.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.

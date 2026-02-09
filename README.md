# Md → Confluence Publisher (Data Center)

Это **только** публикатор. Без ваших доков. Без локальной Confluence. Без `.idea`.  
Потому что “всё в одном архиве” удобно только если вы коллекционируете боль.

## Что делает

- Берёт дерево `docs/**/*.md` в репе с документацией.
- Для каждой папки создаёт “страницу-раздел”.
- Для каждого `.md` создаёт/обновляет страницу Confluence.
- Переписывает markdown-ссылки между файлами в ссылки между Confluence-страницами.
- (Опционально) добавляет TOC и нумерует заголовки.


## Как публикатор узнаёт "свои" страницы

- Ставит странице **один** видимый label (по умолчанию `managed-docs`).
  Его можно переопределить в `publish.yml`, чтобы разные команды могли шарить один space/root, но не наступать друг другу на горло:

  ```yml
  options:
    managed_label: team-a-docs
  ```
- Источник страницы (ключ файла/директории и его хэш), а также **технические классификаторы** (md/dir/section) хранит в **content properties** Confluence (`md2conf_source`). Это обычным пользователям в UI не видно.
- Старый режим (label вида `src-xxxxxxxxxxxx`, а также `md`/`dir`/`section`) автоматически мигрируется: при первом прогоне такие labels удаляются, если у страницы уже есть content property с source key.

## Требования

- Confluence **Data Center** с включённым REST API
- Токен/логин, который может создавать/обновлять страницы в нужном Space
- Либо Python 3.12+, либо Docker

---

## Быстрый старт: локально (без Docker)

1) Склонируй **репу с доками** (docs-repo) и положи туда `publish.yml` (пример в `examples/publish.example.yml`).

2) В этой (publisher) репе:

```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
```

3) Запуск из **корня docs-repo**:

```powershell
$env:CONF_TOKEN="..."
$env:CONF_BASE_URL="http://localhost:8090"
$env:CONF_SPACE="DOC"
$env:CONF_DOCS_ROOT_ID="884737"

python C:\path\to\md-to-conf-publisher\publish_docs.py --pass 2 --cfg publish.yml
```

---

## Быстрый старт: локально (Docker, максимально похоже на CI)

### 1) Собери образ

```powershell
cd C:\path\to\md-to-conf-publisher
docker build -t publisher-to-conf:dev .
```

### 2) Подготовь `.env` в **docs-repo**

Скопируй `examples/env.example` в docs-repo как `.env` и заполни.

ВАЖНО: если Confluence запущена на хосте, в контейнере **нельзя** использовать `localhost`.
Используй `http://host.docker.internal:8090`.

### 3) Запусти

```powershell
cd C:\path\to\docs-repo

docker run --rm --env-file .\.env `
  -v "${PWD}:/work" -w /work `
  publisher-to-conf:dev `
  --pass 2 --cfg publish.yml
```

Проверка, что контейнер видит Confluence:

```powershell
docker run --rm curlimages/curl:8.5.0 -sS -o /dev/null -w "HTTP=%{http_code}`n" http://host.docker.internal:8090/
```

`HTTP=302` или `HTTP=200` — всё норм.

---

## Как устроены проходы

- `--pass 1`: создаёт/обновляет страницы без переписывания ссылок
- `--pass 2`: делает **двухфазно внутри одного запуска**:
  - Phase A: ensure всех страниц и заполнение карты `path → pageId`
  - Phase B: переписывание ссылок и финальный update body  
  Поэтому второй запуск `--pass 2` больше не нужен.

---

## Конфиг `publish.yml`

Публикатор читает конфиг из docs-repo. Пример: `examples/publish.example.yml`.

### Приоритет настроек
Если значения заданы и в `publish.yml`, и в env (`CONF_BASE_URL`, `CONF_SPACE`, `CONF_DOCS_ROOT_ID`) — **env побеждает**.

---

## Нумерация заголовков

Если вы не хотите включать нумерацию в Confluence (плагины/макросы), можно включить нумерацию прямо в тексте:

```yml
options:
  heading_numbering: true
  heading_numbering_max_level: 3
```

Публикатор:
- убирает ручную нумерацию из `.md` вида `1.`, `1.1`, `1)`
- добавляет свою (1., 1.1., 1.1.1.) до указанного уровня

Замечание, чтобы не было сюрпризов:
- если нумерация добавляется в текст заголовка, она будет видна и в TOC (это одна строка текста).
- если нужен TOC без цифр, тогда придётся либо жить без нумерации в тексте, либо использовать Confluence-плагин для нумерации.

---

## Команды

Публикация всего дерева:
```powershell
python .\publish_docs.py --pass 2 --cfg publish.yml
```

Публикация **только изменившихся страниц** (MR mode):

1) В CI (или локально) сформируй `changed.txt`.

Формат на строку:
- `<path>` (трактуется как M)
- `A <path>` / `M <path>` / `D <path>`
- `R <old> <new>` или `R100 <old> <new>` (rename)

2) Запусти публикатор:

```powershell
python .\publish_docs.py --pass 2 --cfg publish.yml --paths-file changed.txt
```

Замечания:
- `D` пока игнорируется (страницы не удаляем).
- Для `R` (rename) публикатор обновит **ту же** Confluence-страницу и перепишет source-key, чтобы не плодить дубли.

Публикация одного файла:
```powershell
python .\publish_one.py docs\strategy\playbook\Playbook-0001.md --pass 2 --cfg publish.yml
```

Удалить только то, что создавал публикатор (по label из `options.managed_label`, по умолчанию `managed-docs`) под корнем docs_root:
```powershell
python .\cleanup_managed.py --list-only --cfg publish.yml
python .\cleanup_managed.py --delete --cfg publish.yml
```

---

## Тесты

Тесты нужны, чтобы публикатор не ломался из-за мелких правок.

Установка зависимостей для разработки:

```powershell
pip install -r requirements.txt -r requirements-dev.txt
```

Запуск:

```powershell
pytest
```


## Troubleshooting

### Docker: Confluence runs on host, but container cannot reach `http://localhost:8090`
Inside a container, `localhost` is the container itself. Use:

- `CONF_BASE_URL=http://host.docker.internal:8090` (Docker Desktop)

Quick check:

```powershell
docker run --rm curlimages/curl:8.5.0 -sS -o /dev/null -w "HTTP=%{http_code}`n" http://host.docker.internal:8090/
```

### Heading numbering in text
If `options.heading_numbering=true`, numbers are inserted into heading *text* (H1-H3 by default). That means the TOC macro will also show these numbers, because it reads the heading text.

### Про host.docker.internal в ссылках
Если вы запускаете публикатор в Docker и Confluence крутится на хосте, вы будете использовать
`CONF_BASE_URL=http://host.docker.internal:8090` для доступа к REST API.

Это **не попадёт** в опубликованный контент: ссылки между страницами генерируются как **относительные**
`/pages/viewpage.action?pageId=...` (с учётом возможного context path вроде `/wiki`).

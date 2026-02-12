
# Публикатор Markdown-документации в Confluence.

Текущая версия поддерживает **инкрементальную публикацию**: в CI/CD можно обновлять **только изменённые/новые файлы из MR** (без пересборки всей репы), включая **rename** без создания дублей страниц.

---

## Что делает

- Создаёт/обновляет страницы Confluence из `docs/**/*.md`
- Поддерживает 2 режима запуска:
  - **pass 1**: создаёт/обновляет страницы, выставляет заголовки/родителей, сохраняет метаданные источника
  - **pass 2**: выполняет публикацию и затем переписывает относительные ссылки на корректные Confluence-ссылки
- Ведёт устойчивое соответствие `md-файл -> pageId` через content property:
  - `md2conf_source.key = file:<path>` (например `file:docs/product offering/ADR/ADR-0001.md`)

> В CI обычно запускают **только `--pass 2`**, т.к. он включает полный цикл (upsert + rewrite links).

---

## Требования

- Python 3.11+ (если запускать без контейнера)
- Доступ к Confluence (URL + токен/учётка) с правами на создание/редактирование страниц в целевом Space
- Репозиторий с документацией в `docs/`

---

## Настройка

### 1) Переменные окружения

Минимально необходимые (названия могут отличаться в твоём `publish.yml`):

- `CONF_BASE_URL` — базовый URL Confluence (например `http://localhost:8090`)
- `CONF_SPACE_KEY` — ключ space (например `DOC`)
- `CONF_AUTH_*` — токен/логин-пароль (как у вас принято)
- при необходимости: `CONF_PARENT_PAGE_ID` (если корневую страницу задаёшь явно)

Рекомендуется хранить в `.env` и передавать в контейнер `--env-file .env`.

### 2) Конфиг `publish.yml`

В `publish.yml` задаются:
- корневая директория с доками (`docs/`)
- правила маппинга “папка -> раздел”
- шаблоны заголовков/родителей
- настройки Confluence клиента

---

## Запуск

### Локально (Python)

```bash
python publish_docs.py --pass 2 --cfg publish.yml
````

### Через Docker

```bash
docker run --rm --env-file .env \
  -v "$PWD:/work" -w /work \
  md-to-conf-publisher:dev \
  --pass 2 --cfg publish.yml
```

---

## Инкрементальная публикация (MR / changed-only)

Чтобы не публиковать всю репу, публикатор принимает список изменённых файлов через `--paths-file changed.txt`.

### Формат `changed.txt` (важно для путей с пробелами)

**Рекомендуемый формат:** TAB-разделённые строки (как у `git diff --name-status`).
Это гарантирует корректную обработку путей с пробелами, например:
`docs/product offering/features/Feature-0001.md`.

Поддерживаются строки:

* `A<TAB>docs/.../new.md`
* `M<TAB>docs/.../changed.md`
* `D<TAB>docs/.../deleted.md` (сейчас игнорируется, удаление страниц не делаем)
* `R100<TAB>docs/old name.md<TAB>docs/new name.md` (rename без дублей страниц)

Также поддерживаются (для ручного ввода):

* просто путь (трактуется как `M`, путь может содержать пробелы):

  * `docs/product offering/features/Feature-0001.md`
* `A|M|D <путь>` (путь может содержать пробелы):

  * `M docs/product offering/features/Feature-0001.md`
* rename без TAB **требует кавычек**, если есть пробелы:

  * `R "docs/old name.md" "docs/new name.md"`

### Запуск

```bash
python publish_docs.py --pass 2 --cfg publish.yml --paths-file changed.txt
```

Поведение:

* `A/M` → создаёт/обновляет соответствующие страницы
* `R/Rxxx` → обновляет **существующую страницу старого файла** и перепривязывает её к новому пути (обновляет `md2conf_source.key`), чтобы не плодить дубли
* `D` → игнорируется

---

## Пример генерации changed.txt в GitLab CI (с пробелами в путях)

```bash
BASE_SHA="${CI_MERGE_REQUEST_DIFF_BASE_SHA:-$CI_COMMIT_BEFORE_SHA}"
HEAD_SHA="$CI_COMMIT_SHA"

git diff --name-status -M "$BASE_SHA" "$HEAD_SHA" -- docs \
  | awk -F $'\t' '
      ($1=="A" || $1=="M" || $1=="D") && $2 ~ /\.md$/ { print $0 }
      $1 ~ /^R/ && $2 ~ /\.md$/ && $3 ~ /\.md$/ { print $0 }
    ' \
  > changed.txt
```

Дальше запуск контейнера:

```bash
docker run --rm --env-file .env \
  -v "$PWD:/work" -w /work \
  md-to-conf-publisher:dev \
  --pass 2 --cfg publish.yml --paths-file changed.txt
```

---

## Важные заметки

* `_index.md` и `README.md` внутри `docs/` обычно используются как “разделы/индексы” и могут обрабатываться отдельно правилами конфига.
* Для корректного `pass 2` публикатор подтягивает маппинги уже опубликованных страниц из Confluence через property `md2conf_source.key`, поэтому ссылки резолвятся даже если в MR публикуется только часть файлов.

---

## Troubleshooting

* **Ссылки не резолвятся в pass2**

  * проверь, что у опубликованных страниц есть property `md2conf_source.key = file:...`
  * проверь, что `changed.txt` реально содержит нужные `.md` (и что в CI не потерялись TAB-разделители)

* **После rename появился дубль**

  * проверь, что в `changed.txt` попало событие `R... old new`, а не только новый путь как `A/M`
  * включи детект rename в diff: `git diff -M`

* **403/401 от Confluence**

  * проверь токен/права на space и родительские страницы


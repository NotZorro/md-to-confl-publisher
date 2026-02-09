
# md-to-conf-publisher

Публикатор Markdown-документации в Confluence.  
Текущая версия поддерживает **инкрементальную публикацию**: в CI/CD можно обновлять **только изменённые/новые файлы из MR** (без пересборки всей репы), включая **rename** без создания дублей страниц.

---

## Что делает

- Создаёт/обновляет страницы Confluence из `docs/**/*.md`
- Поддерживает 2 прохода:
  - **pass 1**: создаёт/обновляет страницы, выставляет заголовки/родителей, сохраняет метаданные источника
  - **pass 2**: переписывает относительные ссылки в опубликованных страницах на корректные Confluence-ссылки
- Ведёт устойчивое соответствие `md-файл -> pageId` через content property:
  - `md2conf_source.key = file:<path>` (например `file:docs/product offering/ADR/ADR-0001.md`)

---

## Требования

- Python 3.11+ (если запускать без контейнера)
- Доступ к Confluence (URL + токен/учётка) с правами на создание/редактирование страниц в целевом Space
- Репозиторий с документацией в `docs/`

---

## Настройка

### 1) Переменные окружения

Минимально необходимые (названия могут отличаться в твоём `publish.yml`, смотри раздел ниже):

- `CONF_BASE_URL` — базовый URL Confluence (например `http://localhost:8090`)
- `CONF_SPACE_KEY` — ключ space (например `DOC`)
- `CONF_AUTH_*` — токен/логин-пароль (как у тебя принято)
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

> Обычно в CI запускают `--pass 2`, потому что он включает логику публикации + обновление ссылок.

---

## Инкрементальная публикация (для MR / changed-only)

Чтобы не публиковать всю репу, публикатор умеет принимать список изменённых файлов.

### Формат `changed.txt`

Поддерживаются строки:

* Просто путь (трактуется как `M`):

  * `docs/path/to/file.md`

* Явные статусы:

  * `A docs/path/to/new.md`
  * `M docs/path/to/changed.md`
  * `D docs/path/to/deleted.md` (сейчас игнорируется, удаление страниц не делаем)

* Rename:

  * `R docs/old.md docs/new.md`
  * `R100 docs/old.md docs/new.md` (git diff иногда так пишет)

### Запуск changed-only

```bash
python publish_docs.py --pass 2 --cfg publish.yml --paths-file changed.txt
```

Поведение:

* `A/M` → создаёт/обновляет соответствующие страницы
* `R/Rxxx` → обновляет **существующую страницу старого файла** и перепривязывает её к новому пути (обновляет `md2conf_source.key`), чтобы не плодить дубли
* `D` → игнорируется

---

## Пример генерации changed.txt в GitLab CI

```bash
BASE_SHA="$CI_MERGE_REQUEST_DIFF_BASE_SHA"
HEAD_SHA="$CI_COMMIT_SHA"

git diff --name-status "$BASE_SHA" "$HEAD_SHA" -- docs \
  | awk '
      $1=="M" || $1=="A" {print $1" "$2}
      $1 ~ /^R/ {print $1" "$2" "$3}
      $1=="D" {print $1" "$2}
    ' \
  | grep -E '\.md($| )' \
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
* Для корректного pass2 (переписывание ссылок) публикатор подтягивает маппинги уже опубликованных страниц из Confluence через property `md2conf_source.key`. Поэтому ссылки резолвятся, даже если в MR публикуется только часть файлов.

---

## Troubleshooting

* **Ссылки не резолвятся в pass2**

  * проверь, что у опубликованных страниц есть property `md2conf_source.key = file:...`
  * убедись, что `--pass 2` запускается после (или вместе с) pass1 логикой публикации

* **После rename появился дубль**

  * проверь, что в `changed.txt` попало событие `R ... ...`, а не только новый путь как `A`
  * если git diff не даёт rename, включи его детект (например, `git diff -M`)

* **403/401 от Confluence**

  * проверь токен/права на space и родительские страницы


# Monitoring Request Manager

Nextcloud ExApp для управления сборами файлов от образовательных организаций.

## Что реализовано

- ExApp metadata в `appinfo/info.xml` для установки через Nextcloud AppAPI.
- Docker image contract для Deploy Daemon типа HaRP Proxy (Docker).
- FastAPI backend с lifecycle, AppAPI middleware, SQLite-хранилищем и статическим UI.
- Справочник организаций, импорт/экспорт CSV/XLSX, кампании, выбор организаций.
- WebDAV-клиент для `PROPFIND`, `MKCOL`, `MOVE`, `COPY`, `DELETE`, `PUT` через AppAPI-аутентификацию текущего пользователя.
- OCS Share API клиент для публичных upload-ссылок.
- Preview/apply для создания папок, переименований и перемещений.
- Проверка файлов кампании, нормализация имён, точное сопоставление кодов, дубли.
- XLSX/CSV рассылки, XLSX-отчёт, текст списка не предоставивших файлы.
- Audit log и operation batches/items.

## Важные ограничения

- Основной режим работает только через WebDAV и OCS Share API.
- Прямой доступ к data-директории Nextcloud не используется.
- `occ files:scan`, `chown`, `chmod`, `os.rename`, `shutil.move` не применяются для файлов Nextcloud.
- Массовые изменения требуют preview и отдельного apply.
- Все операции ограничиваются корневой папкой `MRM_ROOT_PATH`.

## Установка как ExApp

Приложение рассчитано на инфраструктуру:

- Nextcloud запущен в Docker.
- AppAPI установлен и включён.
- HaRP Deploy Daemon запущен в той же Docker-сети.
- ExApp контейнер запускается AppAPI/HaRP в этой же сети.
- Внешний проброс портов ExApp не требуется: трафик идёт через HaRP/FRP.

Перед публикацией замените в `appinfo/info.xml`:

```xml
<registry>ghcr.io</registry>
<image>example/monitoring-request-manager</image>
<image-tag>0.1.0</image-tag>
```

на ваш registry/image/tag, затем соберите и опубликуйте контейнер.

## Переменные окружения

- `MRM_ROOT_PATH`: корневая папка мониторингов, по умолчанию `/Мониторинги`.
- `MRM_DATABASE_URL`: SQLAlchemy URL, по умолчанию SQLite в volume `/data`.
- `MRM_DUPLICATES_FOLDER`: папка дублей, по умолчанию `_Дубли`.
- `MRM_INVALID_FOLDER`: папка неподходящих файлов, по умолчанию `_Неподходящие`.
- `MRM_DEFAULT_EXTENSIONS`: список расширений через запятую.

## Локальная проверка кода

```powershell
python -m pip install -r requirements-dev.txt
pytest
```

В этом рабочем окружении полноценный запуск ExApp не проверяется, потому что нет Nextcloud/AppAPI/HaRP сервера.

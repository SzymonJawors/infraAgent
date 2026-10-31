# InfraAgent

## Polski

InfraAgent zamienia polecenie w języku naturalnym na konfigurację Terraform dla AWS. Każda sesja ma własny katalog roboczy. Wygenerowana konfiguracja jest formatowana, inicjalizowana i walidowana. Gdy skonfigurowane są poświadczenia AWS, to samo żądanie zwraca także `terraform plan`.

`apply` i `destroy` nie są dostępne. Te operacje pozostają za jawnym zatwierdzeniem konkretnego planu; ten krok nie jest jeszcze zaimplementowany.

### Zakres

- `GET /health` zwraca `{"status":"ok"}`, gdy proces działa.
- `POST /sessions/{session_id}/messages` przyjmuje `{"message": "..."}` i zapisuje pliki `.tf` w `workspace/{session_id}/`.
- Gdy dostępny jest Docker albo binarka `terraform-mcp-server`, agent korzysta z publicznej dokumentacji Terraform Registry przed zapisem plików. Gdy to źródło jest niedostępne, generowanie trwa dalej, a odpowiedź ustawia `registry_docs` na `false`.
- Po zapisie plików agent uruchamia `terraform fmt`, `terraform init -backend=false` i `terraform validate`. Przy obecnych poświadczeniach AWS uruchamia następnie `terraform plan`.
- Błąd konfiguracji z `validate` albo `plan` jest poprawiany w wygenerowanym HCL i sprawdzany ponownie, najwyżej trzy razy. Brak CLI, timeout pobierania providera i brak poświadczeń AWS kończą pętlę poprawek.
- `local-exec`, `remote-exec`, provisionery oraz `data "external"` są odrzucane przed zapisem.
- Kolejna wiadomość w tej samej sesji odczytuje pliki już zapisane dla tej sesji.

`session_id` to pojedyncza nazwa katalogu: litery, cyfry, `_` i `-`, najwyżej 64 znaki.

### Interfejs HTTP

| Warunek | Wynik |
| --- | --- |
| Brak poświadczenia modelu językowego | `503` |
| Niepoprawny `session_id` | `400` |
| Brak `terraform` na `PATH` | Pliki zostają. `validation.missing_binary` ma wartość `true`. |
| Brak poświadczeń AWS | Walidacja nadal się wykonuje. `plan` jest pomijany i nie jest wysyłany do AWS. |

Treść odpowiedzi zawiera `reply`, `files`, `validation`, `plan` i `registry_docs`.

Przykład:

```http
POST /sessions/demo/messages
Content-Type: application/json

{"message":"Prywatny bucket S3 na backupy, z szyfrowaniem i blokadą publicznego dostępu"}
```

### Wymagania

- Python 3.14
- Terraform na `PATH`, jeśli mają działać walidacja i plan
- Opcjonalnie Docker albo binarka `terraform-mcp-server`, do dokumentacji rejestru

Poświadczenia AWS są wymagane wyłącznie dla `terraform plan`.

### Instalacja

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
uvicorn app.main:app --reload
```

Usługa nasłuchuje na [http://127.0.0.1:8000](http://127.0.0.1:8000). Kontrola życia: [http://127.0.0.1:8000/health](http://127.0.0.1:8000/health).

Obraz Dockera uruchamia proces API. Nie zawiera CLI Terraform ani skanerów bezpieczeństwa.

### Konfiguracja

Skopiować `.env.example` do `.env`. Pliku `.env` nie należy commitować.

| Zmienna | Przeznaczenie |
| --- | --- |
| `OPENAI_API_KEY` | Poświadczenie modelu językowego. Nie trafia do plików `.tf` ani do logów. |
| `OPENAI_MODEL` | Opcjonalny identyfikator modelu. |
| `AWS_ACCESS_KEY_ID` | Przekazywany wyłącznie procesowi Terraform. |
| `AWS_SECRET_ACCESS_KEY` | Przekazywany wyłącznie procesowi Terraform. |
| `AWS_REGION` | Region AWS. Domyślnie `eu-central-1`. |

### Bezpieczeństwo

Poświadczenia AWS pozostają w środowisku procesu Terraform. Nie trafiają do promptu modelu ani do wygenerowanych plików. Wartości z `.env` są usuwane ze szczegółów błędów API. Provisionery i zewnętrzne źródła danych, które mogłyby uruchomić program podczas `plan`, są odrzucane przy zapisie.

### Dalszy zakres

- `apply` i `destroy`, każde za jawnym zatwierdzeniem konkretnego planu
- Skan Checkov albo tfsec
- Obraz kontenera zawierający CLI Terraform i skanery

## English

InfraAgent turns a natural-language infrastructure request into Terraform for AWS. Each session keeps its own workspace. Generated configuration is formatted, initialized, and validated. When AWS credentials are configured, the same request also produces a `terraform plan`.

`apply` and `destroy` are not available. Those operations stay behind an explicit approval step that is not implemented yet.

## Capabilities

- `GET /health` returns `{"status":"ok"}` when the process is running.
- `POST /sessions/{session_id}/messages` accepts `{"message": "..."}` and writes `.tf` files under `workspace/{session_id}/`.
- When Docker or the `terraform-mcp-server` binary is available, the agent consults public Terraform Registry documentation before writing files. If that source is unavailable, generation continues and the response sets `registry_docs` to `false`.
- After the files are written, the agent runs `terraform fmt`, `terraform init -backend=false`, and `terraform validate`. With AWS credentials present, it then runs `terraform plan`.
- A configuration error from `validate` or `plan` is corrected in the generated HCL and checked again, up to three times. Missing CLI, a provider download timeout, and missing AWS credentials stop the retry loop.
- `local-exec`, `remote-exec`, provisioners, and `data "external"` are rejected before they are written.
- A later message in the same session reads the files already stored for that session.

`session_id` is a single directory name: letters, digits, `_`, and `-`, at most 64 characters.

## HTTP interface

| Condition | Result |
| --- | --- |
| Missing language-model credential | `503` |
| Invalid `session_id` | `400` |
| `terraform` missing from `PATH` | Files are kept. `validation.missing_binary` is `true`. |
| AWS credentials absent | Validation still runs. `plan` is skipped and is not sent to AWS. |

The response body contains `reply`, `files`, `validation`, `plan`, and `registry_docs`.

Example:

```http
POST /sessions/demo/messages
Content-Type: application/json

{"message":"Private S3 bucket for backups, encrypted, with public access blocked"}
```

## Requirements

- Python 3.14
- Terraform on `PATH` for validation and plan
- Optional: Docker, or a `terraform-mcp-server` binary, for registry documentation

AWS credentials are required only for `terraform plan`.

## Installation

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
uvicorn app.main:app --reload
```

The service listens on [http://127.0.0.1:8000](http://127.0.0.1:8000). Health check: [http://127.0.0.1:8000/health](http://127.0.0.1:8000/health).

The Docker image runs the API process. It does not include the Terraform CLI or security scanners.

## Configuration

Copy `.env.example` to `.env`. Do not commit `.env`.

| Variable | Purpose |
| --- | --- |
| `OPENAI_API_KEY` | Credential for the language model. It is not written into `.tf` files or logs. |
| `OPENAI_MODEL` | Optional model identifier. |
| `AWS_ACCESS_KEY_ID` | Passed only to the Terraform process. |
| `AWS_SECRET_ACCESS_KEY` | Passed only to the Terraform process. |
| `AWS_REGION` | AWS region. Default: `eu-central-1`. |

## Security

AWS credentials stay in the process environment used by Terraform. They are not placed in the model prompt or in generated files. Values from `.env` are redacted from API error details. Provisioners and external data sources that could execute a program during `plan` are rejected at write time.

## Roadmap

- `apply` and `destroy`, each gated by an explicit approval of a specific plan
- Checkov or tfsec scanning
- A container image that includes the Terraform CLI and scanners

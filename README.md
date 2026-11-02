# InfraAgent

## Polski

InfraAgent zamienia polecenie w języku naturalnym na konfigurację Terraform dla AWS. Każda sesja ma własny katalog roboczy. Wygenerowana konfiguracja jest formatowana, inicjalizowana i walidowana. Gdy skonfigurowane są poświadczenia AWS, to samo żądanie zwraca także `terraform plan`.

`apply` i `destroy` nie wychodzą z czatu. Każde wymaga osobnego żądania HTTP z `{"approve": true}` i skrótu plików z ostatniego planu tego rodzaju.

### Zakres

- `GET /health` zwraca `{"status":"ok"}`, gdy proces działa.
- `POST /sessions/{session_id}/messages` przyjmuje `{"message": "..."}` i zapisuje pliki `.tf` w `workspace/{session_id}/`.
- Gdy dostępny jest Docker albo binarka `terraform-mcp-server`, agent korzysta z publicznej dokumentacji Terraform Registry przed zapisem plików. Gdy to źródło jest niedostępne, generowanie trwa dalej, a odpowiedź ustawia `registry_docs` na `false`.
- Po zapisie plików agent uruchamia `terraform fmt`, `terraform init -backend=false` i `terraform validate`, a potem skan Checkov. Przy obecnych poświadczeniach AWS uruchamia następnie `terraform plan`.
- Błąd konfiguracji z `validate`, skanu albo `plan` jest poprawiany w wygenerowanym HCL i sprawdzany ponownie, najwyżej trzy razy. Brak CLI, timeout pobierania providera i brak poświadczeń AWS kończą pętlę poprawek. Skan niczego nie wdraża.
- `local-exec`, `remote-exec`, provisionery oraz `data "external"` są odrzucane przed zapisem.
- Kolejna wiadomość w tej samej sesji odczytuje pliki już zapisane dla tej sesji i kasuje poprzednie potwierdzenia planu.
- Odpowiedź opisuje zasoby i powód ich ustawień. Przy błędzie konfiguracji opisuje komunikat i poprawkę.

`session_id` to pojedyncza nazwa katalogu: litery, cyfry, `_` i `-`, najwyżej 64 znaki.

### Zatwierdzenie

Po udanym `terraform plan` i udanym skanie Checkov API zapisuje skrót plików `*.tf`. Nowa wiadomość ten skrót unieważnia. Nieudany skan nie tworzy potwierdzenia i nie uruchamia `apply`.

- `POST /sessions/{session_id}/apply` z `{"approve": true}` uruchamia `terraform apply`, gdy skrót planu apply nadal pasuje do plików.
- `POST /sessions/{session_id}/plans/destroy` uruchamia `terraform plan -destroy` i niczego nie kasuje. Przy sukcesie zapisuje osobny skrót.
- `POST /sessions/{session_id}/destroy` z `{"approve": true}` uruchamia `terraform destroy` tylko dla skrótu planu destroy.

Brak zatwierdzenia daje `400`. Brak poświadczeń AWS daje `503`. Brak planu albo inny skrót plików daje `409`. AWS nie jest wtedy wywoływany. Wiadomość w czacie, także „tak”, nie zastępuje tych żądań.

### Interfejs HTTP

| Warunek | Wynik |
| --- | --- |
| Brak poświadczenia modelu językowego | `503` |
| Niepoprawny `session_id` | `400` |
| Brak `terraform` na `PATH` | Pliki zostają. `validation.missing_binary` ma wartość `true`. |
| Brak poświadczeń AWS | Walidacja nadal się wykonuje. `plan` jest pomijany i nie jest wysyłany do AWS. |

Treść odpowiedzi zawiera `reply`, `files`, `validation`, `plan`, `scan` i `registry_docs`.

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

Poświadczenia AWS są wymagane dla `terraform plan`, `apply` i `destroy`. Sama walidacja działa bez nich.

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
| `AWS_REGION` | Region procesu Terraform, gdy sam plik go nie ustawia. Domyślnie `eu-central-1`. Agent bierze region z polecenia i pyta, gdy go nie ma. |

### Bezpieczeństwo

Poświadczenia AWS pozostają w środowisku procesu Terraform. Nie trafiają do promptu modelu ani do wygenerowanych plików. Wartości z `.env` są usuwane ze szczegółów błędów API. Provisionery i zewnętrzne źródła danych, które mogłyby uruchomić program podczas `plan`, są odrzucane przy zapisie.

### Dalszy zakres

- Obraz kontenera zawierający CLI Terraform i skanery

### Ewaluacje

`python -m evals.run_eval` puszcza trzy polecenia na modelu, każde w osobnym katalogu tymczasowym. Klucze AWS są na czas przebiegu usuwane, a `apply` nie jest wołane. Wynik liczy lokalny kod: brak sekretów i provisionerów, brak wysłanego planu, a dla pełnego polecenia także walidacja i brak publicznego dostępu. Brak regionu ma skończyć się pytaniem, bez plików. Gdy ustawione jest `BRAINTRUST_API_KEY`, te same wyniki 0/1 trafiają do eksperymentu `InfraAgent`. Bez klucza przebieg zostaje na konsoli.

## English

InfraAgent turns a natural-language infrastructure request into Terraform for AWS. Each session keeps its own workspace. Generated configuration is formatted, initialized, and validated. When AWS credentials are configured, the same request also produces a `terraform plan`.

`apply` and `destroy` do not run from chat. Each one needs its own HTTP request with `{"approve": true}` and a file digest from the latest plan of that kind.

## Capabilities

- `GET /health` returns `{"status":"ok"}` when the process is running.
- `POST /sessions/{session_id}/messages` accepts `{"message": "..."}` and writes `.tf` files under `workspace/{session_id}/`.
- When Docker or the `terraform-mcp-server` binary is available, the agent consults public Terraform Registry documentation before writing files. If that source is unavailable, generation continues and the response sets `registry_docs` to `false`.
- After the files are written, the agent runs `terraform fmt`, `terraform init -backend=false`, and `terraform validate`, then a Checkov scan. With AWS credentials present, it then runs `terraform plan`.
- A configuration error from `validate`, the scan, or `plan` is corrected in the generated HCL and checked again, up to three times. Missing CLI, a provider download timeout, and missing AWS credentials stop the retry loop. The scan does not apply anything.
- `local-exec`, `remote-exec`, provisioners, and `data "external"` are rejected before they are written.
- A later message in the same session reads the files already stored for that session and clears previous plan receipts.
- The reply names the resources and why they are configured that way. A configuration error includes what the message means and what changed.

`session_id` is a single directory name: letters, digits, `_`, and `-`, at most 64 characters.

## Approval

A successful `terraform plan` and a passing Checkov scan store a digest of the `*.tf` files. A new chat message invalidates it. A failed scan does not store the digest and does not run `apply`.

- `POST /sessions/{session_id}/apply` with `{"approve": true}` runs `terraform apply` when the apply-plan digest still matches the files.
- `POST /sessions/{session_id}/plans/destroy` runs `terraform plan -destroy` and deletes nothing. On success it stores a separate digest.
- `POST /sessions/{session_id}/destroy` with `{"approve": true}` runs `terraform destroy` only for the destroy-plan digest.

A missing approval returns `400`. Missing AWS credentials return `503`. A missing plan or a changed digest returns `409`. AWS is not called in those cases. A chat message, including "yes", does not replace these requests.

## HTTP interface

| Condition | Result |
| --- | --- |
| Missing language-model credential | `503` |
| Invalid `session_id` | `400` |
| `terraform` missing from `PATH` | Files are kept. `validation.missing_binary` is `true`. |
| AWS credentials absent | Validation still runs. `plan` is skipped and is not sent to AWS. |

The response body contains `reply`, `files`, `validation`, `plan`, `scan`, and `registry_docs`.

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

AWS credentials are required for `terraform plan`, `apply`, and `destroy`. Validation runs without them.

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
| `AWS_REGION` | Region for the Terraform process when a file does not set one. Default: `eu-central-1`. The agent takes the region from the request and asks when it is missing. |

## Security

AWS credentials stay in the process environment used by Terraform. They are not placed in the model prompt or in generated files. Values from `.env` are redacted from API error details. Provisioners and external data sources that could execute a program during `plan` are rejected at write time.

## Roadmap

- A container image that includes the Terraform CLI and scanners

## Evaluations

`python -m evals.run_eval` sends three requests to the model, each in its own temporary directory. AWS credentials are removed for the run, and `apply` is not called. Local checks score the result: no secrets, no provisioners, no plan sent to AWS, and, for a complete request, a passing validation and no public access. A request without a region should end as one question and no files. When `BRAINTRUST_API_KEY` is set, the same 0/1 scores are sent to an `InfraAgent` experiment. Without the key, the run stays on the console.

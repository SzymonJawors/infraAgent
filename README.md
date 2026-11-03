# InfraAgent

Licensed under the [Apache License 2.0](./LICENSE).

## Polski

InfraAgent zamienia polecenie w języku naturalnym na konfigurację Terraform dla AWS. Każda sesja ma własny katalog roboczy. Wygenerowana konfiguracja jest formatowana, inicjalizowana i walidowana. Gdy skonfigurowane są poświadczenia AWS, to samo żądanie zwraca także `terraform plan`.

`apply` i `destroy` nie wychodzą z czatu. Każde wymaga osobnego żądania HTTP z `{"approve": true}` i skrótu plików z ostatniego planu tego rodzaju.

### Zakres

- `GET /health` zwraca `{"status":"ok"}`, gdy proces działa.
- `POST /sessions/{session_id}` tworzy pustą sesję przed jej użyciem lub zmianą
  nazwy. Tytuł sesji jest niezależny od treści wiadomości czatu.
- `POST /sessions/{session_id}/messages` przyjmuje `{"message": "..."}` i zapisuje pliki `.tf` w `workspace/{session_id}/`. Opcjonalne `skip_checkov: true` pomija skan Checkov tylko dla tego żądania i jest przeznaczone wyłącznie do szybkiego podglądu.
- `POST /sessions/{session_id}/messages/stream` wysyła postęp generowania przez
  Server-Sent Events (`status`, `result`, `error`); zwykły endpoint pozostaje
  dostępny dla klientów bez obsługi streamingu.
- `GET /sessions/{session_id}/agent-status` zwraca trwały status zadania. Praca
  agenta jest kontynuowana po przełączeniu czatu, a frontend odtwarza wynik po
  powrocie do sesji.
- `POST /sessions/{session_id}/agent-cancel` anuluje aktywne zadanie agenta bez
  usuwania istniejących plików Terraform.
- `GET /sessions/{session_id}/operations` pokazuje historię generowania, planów,
  apply i destroy dla sesji.
- `GET /sessions/{session_id}/conversation` zwraca trwale zapisaną historię
  rozmowy z workspace'u sesji; frontend używa jej jako źródła prawdy po
  ponownym otwarciu sesji.
- Frontend używa WebSocket do natychmiastowego postępu; SSE pozostaje dostępne
  jako fallback.
- MCP jest pomijany przy prostych zmianach istniejącego workspace'u i używany
  dla nowych konfiguracji oraz próśb o dokumentację providera.
- Dokumentacja rejestru korzysta z Terraform MCP przez Streamable HTTP. Backend automatycznie uruchamia lokalną binarkę MCP na `http://127.0.0.1:8080/mcp`; `TERRAFORM_MCP_URL` pozwala wskazać własny działający serwer.
- Po zapisie plików agent uruchamia `terraform fmt`, `terraform init -backend=false` i `terraform validate`, a potem skan Checkov. Przy obecnych poświadczeniach AWS uruchamia następnie `terraform plan`. Przy `skip_checkov: true` pomija Checkov i dodatkowe próby naprawy, ale nadal wykonuje walidację Terraform oraz plan.
- Dla RDS agent nie zgaduje `engine_version`, ponieważ dostępne wersje zależą od regionu i klasy instancji. Jeśli wersja nie została potwierdzona, pole jest pomijane.
- Dla złożonych żądań orchestrator rozdziela pracę na Planner Agent (analiza
  kontekstu), Builder Agent (jedyny agent zapisujący Terraform) oraz
  deterministyczne etapy Validator/Security. Planner i dokumentacja MCP mogą
  działać równolegle. Proste żądania korzystają z krótkiej ścieżki bez MCP.
- W ramach jednego żądania powtórne wywołania review dla niezmienionych plików
  korzystają z poprzedniego wyniku. `terraform init` uruchamia się ponownie
  tylko po zmianie `versions.tf`, `provider.tf` albo lockfile.
- Każdy workspace ma nieblokującą blokadę plikową `.infraagent/workspace.lock`.
  Obejmuje ona generowanie, walidację, plan, apply i destroy; równoległa
  operacja na tej samej sesji kończy się kodem `409`.
- Każde żądanie mierzy czas Plannera, Buildera, review, Checkov oraz
  pojedynczych komend Terraform. Czasy są zapisywane w logu `agent_timing` i
  wysyłane do klienta jako końcowy event `metrics`.
- Provider Terraform jest najpierw szukany we wspólnym `.terraform-plugin-cache/`
  w katalogu projektu jako lokalny filesystem mirror. `terraform init` nie wykonuje
  automatycznego upgrade'u. Jeśli providera nie ma w cache, konfiguracja ma fallback
  `direct {}` i Terraform może pobrać go z registry; bez dostępu do registry init
  kończy się czytelnym błędem.
- Błąd konfiguracji z `validate`, skanu albo `plan` jest poprawiany w wygenerowanym HCL i sprawdzany ponownie najwyżej jeden raz (maksymalnie dwa pełne review na żądanie). Agent zatrzymuje się także, gdy pliki nie zmieniły się po poprawce. Nie ogłasza utworzenia infrastruktury, jeśli końcowy review nie przeszedł. Brak CLI, timeout pobierania providera i brak poświadczeń AWS kończą pętlę poprawek. Skan niczego nie wdraża.
- `local-exec`, `remote-exec`, provisionery oraz `data "external"` są odrzucane przed zapisem.
- Kolejna wiadomość w tej samej sesji odczytuje pliki już zapisane dla tej sesji i kasuje poprzednie potwierdzenia planu.
- Ostatnie wiadomości rozmowy są zapisywane prywatnie w workspace sesji, dzięki
  czemu odpowiedzi na pytania doprecyzowujące (np. sam region) zachowują
  kontekst pierwotnej prośby.
- Odpowiedź opisuje zasoby i powód ich ustawień. Przy błędzie konfiguracji opisuje komunikat i poprawkę.

### Struktura projektu

- `app/api/routes.py` zawiera endpointy HTTP i pozostaje cienką warstwą wejściową.
- `app/api/schemas.py` zawiera modele requestów i odpowiedzi API.
- `app/core/` zawiera konfigurację i logging.
- `app/services/` zawiera logikę agenta, Terraform, skanowanie bezpieczeństwa,
  workspace oraz approval.
- Role agenta są rozdzielone na `planner.py` (analiza tylko do odczytu),
  `builder.py` (jedyny etap zapisujący Terraform), `reviewer.py` (walidacja,
  Checkov i plan) oraz `agent.py` jako koordynator.
- `frontend/src/components/` zawiera widoki panelu, a `frontend/src/lib/`
  komunikację z API i typy kontraktu.
- `workspace/` to lokalne katalogi sesji; `tools/` zawiera opcjonalne narzędzia
  uruchamiane przez agenta.

`session_id` to pojedyncza nazwa katalogu: litery, cyfry, `_` i `-`, najwyżej 64 znaki.

### Zatwierdzenie

Po udanym `terraform plan` i udanym skanie Checkov API zapisuje skrót plików `*.tf`. Przy świadomym `skip_checkov: true` skan jest pomijany, ale skrót nadal może zostać zapisany. Nowa wiadomość ten skrót unieważnia. Nieudana walidacja lub plan nie tworzą potwierdzenia i nie uruchamiają `apply`.

- `POST /sessions/{session_id}/apply` z `{"approve": true}` uruchamia `terraform apply`, gdy skrót planu apply nadal pasuje do plików.
- `POST /sessions/{session_id}/plans/destroy` uruchamia `terraform plan -destroy` i niczego nie kasuje. Przy sukcesie zapisuje osobny skrót.
- `POST /sessions/{session_id}/destroy` z `{"approve": true}` uruchamia `terraform destroy` tylko dla skrótu planu destroy.
- Przed zatwierdzonym destroy agent wyłącza ochronę terminacji EC2 (`disable_api_termination`), jeśli była włączona, aby AWS nie blokował usunięcia instancji.

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
- Binarka `tools/terraform-mcp-server.exe` pozostaje awaryjnym trybem lokalnym. Zalecany tryb uruchamia ją jako osobny serwis:

```powershell
.\tools\terraform-mcp-server.exe streamable-http --transport-host 127.0.0.1 --transport-port 8080 --toolsets registry
```

MCP działa jako osobny proces HTTP. Uruchom go przed backendem:

```powershell
.\tools\start-mcp.ps1
```

Sprawdź jego stan przez `http://127.0.0.1:8002/health` — odpowiedź zawiera pole `mcp.reachable`.
Jeśli używasz własnego serwera MCP, ustaw `TERRAFORM_MCP_URL`.

Poświadczenia AWS są wymagane dla `terraform plan`, `apply` i `destroy`. Sama walidacja działa bez nich.

Terraform korzysta najpierw ze współdzielonego cache providerów, a następnie z bezpośredniego
Terraform Registry. Dzięki temu zasoby wymagające dodatkowego providera, np. `archive_file` z
`hashicorp/archive` dla Lambdy, nie kończą się błędem tylko dlatego, że provider nie był wcześniej
pobrany do cache. Dostęp do `registry.terraform.io` nadal musi być możliwy przy pierwszym użyciu.

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

Przy udanym planie agent wykonuje opcjonalne zapytania do AWS Pricing API przez
AWS CLI (`aws pricing get-products`). Cena jest pokazana jako orientacyjny koszt
miesięczny przy 730 godzinach. Integracja wymaga działającego `aws` na `PATH`
i poświadczeń z dostępem do odczytu cen; korzysta z endpointu Pricing w
`us-east-1`, niezależnie od regionu zasobów. Brak odpowiedzi API nie blokuje
planu i jest oznaczany w interfejsie jako `Estimate unavailable`. Ostrzeżenia
heurystyczne pozostają osobno i nie są wyceną.

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
- `POST /sessions/{session_id}/messages` accepts `{"message": "..."}` and writes `.tf` files under `workspace/{session_id}/`. Optional `skip_checkov: true` skips Checkov for that request only and is intended only for a quick preview.
- `POST /sessions/{session_id}/messages/stream` returns Server-Sent Events
  for progress (`status`), the final response (`result`), or a safe error
  (`error`). The regular endpoint remains available for non-streaming clients.
- `GET /sessions/{session_id}/agent-status` returns the persisted job status.
  Agent work continues when switching chats, and the frontend restores the result
  when returning to the session.
- `POST /sessions/{session_id}/agent-cancel` cancels the active agent job without
  deleting existing Terraform files.
- `GET /sessions/{session_id}/operations` returns generation, plan, apply, and
  destroy history for the session.
- `GET /sessions/{session_id}/conversation` returns the persisted conversation
  history from the session workspace. The frontend uses it as the source of
  truth when reopening a session and falls back to local storage only when the
  API is unavailable.
- The frontend uses WebSocket for immediate progress; SSE remains available as a
  fallback.
- MCP is skipped for simple edits to an existing workspace and used for new
  configurations or provider documentation requests.
- Registry documentation uses Terraform MCP over Streamable HTTP. The backend automatically starts the bundled MCP binary at `http://127.0.0.1:8080/mcp`; `TERRAFORM_MCP_URL` can point to an existing service.
- After the files are written, the agent runs `terraform fmt`, `terraform init -backend=false`, and `terraform validate`, then a Checkov scan. With AWS credentials present, it then runs `terraform plan`. With `skip_checkov: true`, Checkov and its extra repair attempts are skipped while Terraform validation and the plan still run.
- For RDS, the agent does not guess `engine_version` because available versions depend on region and instance class. It omits the field unless the version was confirmed.
- For complex requests, the orchestrator separates a Planner Agent (context
  analysis), a Builder Agent (the only agent allowed to write Terraform), and
  deterministic Validator/Security stages. Planner analysis and MCP
  documentation can run in parallel. Simple requests use the short path
  without MCP.
- Within one request, repeated reviews of unchanged files reuse the previous
  result. `terraform init` runs again only when `versions.tf`, `provider.tf`,
  or the provider lock file changes.
- Each workspace has a non-blocking file lease at
  `.infraagent/workspace.lock`. It covers generation, validation, plan, apply,
  and destroy; a concurrent operation on the same session returns `409`.
- Terraform first looks for providers in the shared project `.terraform-plugin-cache/`
  directory as a local filesystem mirror and does not automatically upgrade them.
  If a provider is missing, the `direct {}` fallback may download it from the
  registry; without registry access, initialization fails clearly.
- When the agent already ran a complete infrastructure check during the request, the final review reuses that result unless the Terraform files changed afterwards.
- A configuration error from `validate`, the scan, or `plan` is corrected in the generated HCL and checked once more, for at most two complete reviews per request. The agent also stops when a repair does not change the files and never claims the infrastructure was created when the final review failed. Missing CLI, a provider download timeout, and missing AWS credentials stop the retry loop. The scan does not apply anything.
- `local-exec`, `remote-exec`, provisioners, and `data "external"` are rejected before they are written.
- A later message in the same session reads the files already stored for that session and clears previous plan receipts.
- Recent conversation turns are stored privately in the session workspace so
  answers to clarifying questions (for example, a region alone) retain the
  original request context.
- Small requests may use `main.tf`; multi-component infrastructure is split into focused files such as `versions.tf`, `provider.tf`, `network.tf`, `compute.tf`, `database.tf`, `storage.tf`, and `outputs.tf`. Terraform loads these files together from the session directory.
- For the default production-oriented security baseline, the agent prioritizes RDS PostgreSQL TLS enforcement and least-privilege KMS policies. S3 replication, access-logging buckets, and event notifications remain optional unless explicitly requested because they expand scope and cost.
- The default scan policy accepts `CKV_AWS_382` for the minimal EC2 baseline because outbound access is needed for SSM, package repositories, and AWS APIs without a requested VPC endpoint/NAT design. It also accepts the optional S3 scope findings `CKV_AWS_18` (access logging), `CKV2_AWS_61` (lifecycle retention), `CKV_AWS_144` (cross-region replication), and `CKV2_AWS_62` (event notifications) as non-blocking warnings. The scan output explains each acceptance; other Checkov failures continue to block approval.
- The reply names the resources and why they are configured that way. A configuration error includes what the message means and what changed.

`session_id` is a single directory name: letters, digits, `_`, and `-`, at most 64 characters.

## Approval

A successful `terraform plan` and a passing Checkov scan store a digest of the `*.tf` files. With explicit `skip_checkov: true`, the scan is skipped but the digest can still be stored. A new chat message invalidates it. Failed Terraform validation or a failed plan do not store the digest and do not run `apply`.

- `POST /sessions/{session_id}/apply` with `{"approve": true}` runs `terraform apply` when the apply-plan digest still matches the files.
- `POST /sessions/{session_id}/plans/destroy` runs `terraform plan -destroy` and deletes nothing. On success it stores a separate digest.
- `POST /sessions/{session_id}/destroy` with `{"approve": true}` runs `terraform destroy` only for the destroy-plan digest.
- Before an approved destroy, the agent disables EC2 termination protection when it is enabled so AWS can remove the instance.

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
- The `tools/terraform-mcp-server.exe` binary for registry documentation. The chat still runs without it.
- Recommended MCP service command:

```powershell
.\tools\terraform-mcp-server.exe streamable-http --transport-host 127.0.0.1 --transport-port 8080 --toolsets registry
```

The backend starts MCP with FastAPI and stops it during shutdown. Set `TERRAFORM_MCP_URL` only when using an external MCP service.

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

## CI

GitHub Actions runs the Python test suite and the frontend lint/build on every
push and pull request. The Docker image remains a lightweight API image;
Terraform and Checkov are supplied by the local development environment or a
dedicated runtime image.

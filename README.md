# InfraAgent

## Polski

InfraAgent zamienia polecenie w języku naturalnym na pliki Terraform dla AWS. Składnia jest sprawdzana przez `terraform validate`. Wdrożenie (`apply`) i kasowanie (`destroy`) nie startują same — mają ruszyć dopiero po Twoim zatwierdzeniu konkretnego planu. Domyślny model to `gpt-5.4-mini`; w `OPENAI_MODEL` można wpisać dowolny identyfikator modelu OpenAI.

### Co działa teraz

- `GET /health` — życie procesu.
- `POST /sessions/{session_id}/messages` z ciałem `{"message": "..."}` — model OpenAI pisze pliki `.tf` do `workspace/{session_id}/`, a potem agent odpala `terraform fmt`, `init -backend=false` i `validate`.
- Gdy walidacja zwróci błąd składni, agent poprawia HCL i próbuje ponownie (do trzech razy).
- Kolejna wiadomość w tej samej sesji widzi już zapisane pliki.
- Brak klucza OpenAI daje `503`. Brak binarki `terraform` na PATH nie kasuje plików: odpowiedź ma `missing_binary: true`.

`session_id` to jedna nazwa katalogu: litery, cyfry, `_` i `-`, do 64 znaków.

### Czego jeszcze nie ma

- `terraform plan`, `apply` i `destroy` (apply i destroy tylko po jawnym zatwierdzeniu).
- Skan bezpieczeństwa Checkov albo tfsec.
- Osobny kontener na CLI Terraforma i skanery. Obecny obraz Dockera uruchamia samą aplikację.

### Uruchomienie

Potrzebny jest Python 3.14.

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
uvicorn app.main:app --reload
```

- [http://127.0.0.1:8000/health](http://127.0.0.1:8000/health) zwraca `{"status":"ok"}`.
- Przykład: `POST http://127.0.0.1:8000/sessions/demo/messages` z JSON `{"message":"Prywatny bucket S3 na backupy, z szyfrowaniem i blokadą publicznego dostępu"}`.

Na PATH musi być `terraform`, jeśli walidacja ma się udać. Klucze AWS nie są potrzebne do samego `validate`.

### Konfiguracja

Skopiuj `.env.example` do `.env`.

- `OPENAI_API_KEY` — tylko do modelu. Nie trafia do plików `.tf` ani do logów.
- `OPENAI_MODEL` — domyślnie `gpt-5.4-mini`. Może to być dowolny model OpenAI, na przykład `gpt-5.2` albo `gpt-5-mini`.
- `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_REGION` — przekazywane procesowi Terraform, nie modelowi. Domyślny region to `eu-central-1`.

Pliku `.env` nie commituj.

## English

InfraAgent turns a natural-language request into Terraform files for AWS. Syntax is checked with `terraform validate`. `apply` and `destroy` do not run on their own — they are meant to start only after you approve a specific plan. The default model is `gpt-5.4-mini`; set `OPENAI_MODEL` to any OpenAI model id.

### What works now

- `GET /health` — process liveness.
- `POST /sessions/{session_id}/messages` with `{"message": "..."}` — the OpenAI model writes `.tf` files under `workspace/{session_id}/`, then the agent runs `terraform fmt`, `init -backend=false`, and `validate`.
- If validation reports a syntax error, the agent rewrites the HCL and tries again (up to three times).
- A later message in the same session sees the files already on disk.
- A missing OpenAI key returns `503`. A missing `terraform` binary does not delete the files: the response sets `missing_binary: true`.

`session_id` is a single directory name: letters, digits, `_` and `-`, up to 64 characters.

### Not built yet

- `terraform plan`, `apply`, and `destroy` (`apply` and `destroy` only after an explicit approval).
- A Checkov or tfsec security scan.
- A separate container for the Terraform CLI and scanners. The current Docker image runs the app only.

### Run

Python 3.14 is required.

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
uvicorn app.main:app --reload
```

- [http://127.0.0.1:8000/health](http://127.0.0.1:8000/health) returns `{"status":"ok"}`.
- Example: `POST http://127.0.0.1:8000/sessions/demo/messages` with `{"message":"Private S3 bucket for backups, encrypted, public access blocked"}`.

`terraform` must be on `PATH` for validation to succeed. AWS keys are not required for `validate`.

### Configuration

Copy `.env.example` to `.env`.

- `OPENAI_API_KEY` — model only. It is not written into `.tf` files or logs.
- `OPENAI_MODEL` — default `gpt-5.4-mini`. Any OpenAI model id works, for example `gpt-5.2` or `gpt-5-mini`.
- `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_REGION` — passed to the Terraform process, not to the model. Default region is `eu-central-1`.

Do not commit `.env`.

# InfraAgent

Konwersacyjny agent, który opis infrastruktury zamienia w pliki Terraform w `workspace/` i może postawić je na AWS. `terraform plan` wraca do rozmowy. `apply` i `destroy` ruszają dopiero po jawnym potwierdzeniu.

Ten katalog to szkielet na stosie z października 2026: Python 3.14, FastAPI, pydantic-settings i zależności pod LangChain `create_agent` z Gemini (`gemini-4-argon`). Gemini i Terraform CLI nie są jeszcze wołane.

## Uruchomienie

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
uvicorn app.main:app --reload
```

Sprawdzenie: [http://127.0.0.1:8000/health](http://127.0.0.1:8000/health) zwraca `{"status":"ok"}`.

## Konfiguracja

Skopiuj `.env.example` do `.env`.

- `GEMINI_API_KEY` (albo `GOOGLE_API_KEY`) — klucz z Google AI Studio, tylko do modelu.
- `GEMINI_MODEL` — domyślnie `gemini-4-argon`. Lżejszy model pod agenty: `gemini-3.8-flash`.
- `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_REGION` — dla procesu Terraform. Nie trafiają do promptu, do plików `.tf` ani do logów.

Pliku `.env` nie commituj.

## Układ

- `app/main.py` — aplikacja i `GET /health`
- `app/core/config.py` — zmienne z `.env`
- `app/core/logging.py` — logi bez sekretów
- `app/services/agent.py` — miejsce na `create_agent` i Gemini (`ChatGoogleGenerativeAI`)
- `app/services/terraform.py` — jedyne miejsce na wywołania CLI
- `app/services/scanner.py` — odczyt istniejących `.tf`
- `app/api/routes.py` — kolejne endpointy
- `workspace/` — generowane pliki `.tf`

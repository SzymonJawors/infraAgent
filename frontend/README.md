# InfraAgent frontend

Panel webowy InfraAgent jest aplikacją React + TypeScript zbudowaną przez Vite.
Udostępnia czat do generowania Terraform, listę plików sesji, wyniki walidacji
i skanu Checkov oraz bezpieczne akcje `apply` i `destroy`.

## Uruchomienie lokalne

W jednym terminalu uruchom API z katalogu głównego:

```powershell
uvicorn app.main:app --reload
```

W drugim terminalu:

```powershell
cd frontend
npm install
npm run dev
```

Frontend używa `http://localhost:8000` jako domyślnego adresu API. Można go
zmienić przez `VITE_API_URL`, na przykład:

```powershell
$env:VITE_API_URL = "http://127.0.0.1:8000"
npm run dev
```

## Walidacja

```powershell
npm run lint
npm run build
```

## Struktura

- `src/App.tsx` — stan sesji i orkiestracja akcji.
- `src/components/` — widoki panelu: czat, sidebar, inspector i elementy statusu.
- `src/lib/api.ts` — komunikacja z API oraz identyfikator sesji.
- `src/lib/types.ts` — typy kontraktu API.

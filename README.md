# Saal Bikes: bike rental with a RAG chatbot (alpha)

A bike rental website for a fictional company in Ranchi: booking, Razorpay test-mode payments, staff pickup and
return, analytics, live ride tracking on a map, and a chatbot that answers from the customer's own data and from the
company's help documents. A learning project; every user and payment is fake.

Stack: FastAPI, PostgreSQL 17 (PostGIS, pgvector), Redis, React + TypeScript, Docker Compose.

## Run it

Needs Docker Desktop. On Windows run these in Git Bash.

```bash
cp .env.example .env
docker compose up -d --build
bash scripts/garage-init.sh                              # once: file storage for receipts
docker compose exec api python -m app.seed               # once: stations, bikes, four months of fake rides
docker compose exec api python -m app.docs.ingest        # once: index the help documents (downloads ~1.2 GB of models)
```

Open http://localhost:8090. Emails sent by the app land in http://localhost:8090/mail.

| Role | Email | Password |
|---|---|---|
| Customer | asha@example.com | rider12345 |
| Staff | staff.morabadi-ground@saalbikes.test | staff12345 |
| Admin | admin@saalbikes.test | admin12345 |

- **Chat** needs `GROQ_API_KEY` (console.groq.com/keys) or `OPENROUTER_API_KEY` in `.env`, then `docker compose up -d`.
- **Payments** run in a fake mode until `RAZORPAY_KEY_ID` and `RAZORPAY_KEY_SECRET` (test mode) are set.
- **Live map demo:** `docker compose --profile demo up simulator` starts six rides and moves them.

## Tests

```bash
docker build -t bikerental-db:dev db
py -3.13 -m venv .venv && .venv/Scripts/pip install -e "api[dev]"
cd api && ../.venv/Scripts/python -m pytest -q     # needs Docker: the tests start a throwaway Postgres and Redis
cd ../web && npm ci && npm test
```

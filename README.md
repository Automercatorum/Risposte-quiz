# Automercatorum Risposte Quiz

App desktop per macOS che estrae le risposte corrette dei **quiz di esercitazione** ("Test di fine lezione") dalle tue materie sul portale [Universitas Mercatorum](https://lms.mercatorum.multiversity.click/) e le salva in un PDF per materia.

## Avvio

```bash
git clone https://github.com/<tu>/automercatorum-risposte-quiz.git
cd automercatorum-risposte-quiz

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python app.py
```

## Note

- Richiede Python 3.11+
- Solo per quiz di **esercitazione** (non valutati, ripetibili). Strumento di studio personale.
- Credenziali salvate in chiaro in `.auth/creds.json` (permission `0600`, gitignored)
- Rispetta i termini del tuo ateneo.

## Licenza

[MIT](LICENSE)

# Automercatorum Risposte Quiz

App desktop per macOS che estrae le risposte corrette dei **quiz di esercitazione** ("Test di fine lezione") dalle tue materie sul portale [Universitas Mercatorum](https://lms.mercatorum.multiversity.click/) e le salva in un PDF per materia.

Companion del [Downloader di dispense](https://github.com/) e del [Video Export](https://github.com/) — stessa UI, stesso flusso, ma genera un answer key dei quiz di esercitazione.

## Avvio

```bash
git clone https://github.com/<tu>/automercatorum-risposte-quiz.git
cd automercatorum-risposte-quiz

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python app.py
```

Si apre una finestra: login (spunta *Salva credenziali*), seleziona le materie, clicca **Estrai quiz**. Per ogni materia genera `output/<Materia>/quiz_risposte.pdf` con le domande dei test di fine lezione raggruppate per modulo e la risposta corretta evidenziata.

## CLI (opzionale)

```bash
python extract.py --list         # mostra le tue materie
python extract.py <CODICE>       # estrae una materia
python extract.py --all          # tutte
```

## Note

- Richiede Python 3.11+
- Solo per quiz di **esercitazione** (non valutati, ripetibili). Strumento di studio personale.
- Credenziali salvate in chiaro in `.auth/creds.json` (permission `0600`, gitignored)
- Rispetta i termini del tuo ateneo.

## Licenza

[MIT](LICENSE)

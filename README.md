# STA Assistant (Share Transfer Agency chatbot)

Python + OpenAI. Answers come from official sources only.

## Run the chat widget (recommended UI)
```
pip install -r requirements.txt
# put your OpenAI key in .streamlit/secrets.toml (see secrets.toml.example)
uvicorn server:app --reload --port 8000
```
Open http://localhost:8000. A round chat button sits at the bottom-right of the page.

To preview the UI without an OpenAI key, set `DEMO_MODE=1` before starting
(PowerShell: `$env:DEMO_MODE=1`; cmd: `set DEMO_MODE=1`).

To put the assistant on any web page, add one line:
`<script src="http://YOUR-SERVER:8000/widget.js" defer></script>`
(set `ALLOWED_ORIGINS=https://your-site.com` on the server when that page is on a different domain).

## What the user sees
1. A round chat button at the bottom-right of the page.
2. Click it: welcome screen with the topic list and a name box (Start chat stays pale until a name is typed).
3. After the name: topic buttons (KYC, IEPF, Registry, Nomination, Transmission, Dividend, Demat, Find ISIN).
4. Click a topic: its follow-up questions. Click one, or type your own and press send.
5. The header and footer stay fixed; only the messages scroll.
6. Each answer shows only the forms needed for that question, with **View** and **Download** buttons, plus a Sources list.

Edit `topics.json` to change topics and follow-up questions. `BOT_NAME` (environment variable, default "Nia") sets the assistant name.
**Find ISIN** is a placeholder: the company-database lookup is not connected yet (see the TODO in `onChip` in `static/widget.js`).

## Forms (View / Download)
The server fetches the official PDF from the link in `forms.json`, keeps a copy in `form_cache/` for a week and serves it
inline (View) or as an attachment (Download). If the official site cannot be reached, the buttons open the official link instead.
IEPF-5 is a web page, so it shows one **Open** button. DRF has no single official file, so a note is shown instead.

## How an answer is produced
1. **Scope gate** (`sta_bot.classify`): out-of-scope, excluded (bonds, IPO/rights, buyback,
   other corporate actions) and folio-specific questions get a fixed reply. No search happens.
2. **Verified notes** (`verified_kb.json`): matching entries are given to the model as trusted context.
3. **Grounded answer**: OpenAI web search restricted to `ALLOWED_DOMAINS` in `sta_bot.py`
   (sebi.gov.in, nseindia.com, bseindia.com, nsdl.co.in, cdslindia.com, iepf.gov.in,
   mca.gov.in, incometax.gov.in, rbi.org.in). Links outside the list are stripped.
4. **Forms**: the model ends with a `FORMS:` line naming only the forms needed; the app maps them to `forms.json`.
5. **Safety net**: no official source cited -> "could not verify" reply instead of a guess.

## Older Streamlit version
`streamlit run app.py` still works (simpler look, same answer engine).

## Files to review
- `forms.json`: SEBI-hosted links for ISR-1/2/3 (March 2023), ISR-4 (Jan 2026 revision), SH-13/14 (Nov 2021) and the IEPF-5 page.
  SEBI may reissue forms, so re-check after new circulars (`python check_forms.py`).
- `verified_kb.json`: 3 draft answers. Add your most common questions with the circular reference.
- `eval_set.json`: 50 STA questions + 20 out-of-scope questions, mostly without reference answers.

## Measuring accuracy
```
python eval.py --limit 10     # smoke test
python eval.py                # full run -> eval_report.csv
```
Automatic checks only confirm behaviour (answered, cited, refused). Fill the `human_verdict`
column (CORRECT / WRONG / OUTDATED) after checking each answer against the official source.

## Models
Defaults: `gpt-4.1-mini` (scope gate) and `gpt-4.1` (answers). Override with `SCOPE_MODEL` /
`ANSWER_MODEL` in secrets or environment. Your key must have access to the model and to web search.
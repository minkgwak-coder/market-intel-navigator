# Demo video script (2 to 3 minutes)

`docs/demo.mp4` covers the dashboard and the Ask AI pipeline in offline sample mode (captions, no narration). To record the real GPT answer, run with your own OpenAI key. Record it separately (a screen recorder such as the one built into
Windows with Win+Alt+R) and append it, or record the whole thing again following this script with your own voice.

## Setup before recording

1. `pip install -r requirements.txt` and set `OPENAI_API_KEY`.
2. `python server.py`, open the **Ask AI** page once and ask any question about Brazil, so the market is indexed
   (the first question takes about 30 s because statistics and news are collected and embedded).
3. Browser window 1280×720, zoom 100%, close other tabs.

## Shot list

| Time | Screen | Say |
|---|---|---|
| 0:00 | Home page | "Exporters of dental, medical and veterinary devices need to decide where to sell next. The data exists, but it is spread over several tools." |
| 0:15 | Search "Brazil dental", result page | "One search gives import demand from UN Comtrade, health indicators from the World Bank and recent news. Every number shows its year." |
| 0:45 | Chart, suppliers, Korea share | "Imports by year, who supplies them, and where Korea stands." |
| 1:05 | Notice under the title, then footer | "Veterinary devices cannot be separated in trade codes. The page says so instead of guessing." |
| 1:20 | Ask AI page, type "Should Korean dental companies enter Brazil?" | "Now a question. The system parses the market, searches the indexed facts, extracts the relevant data and asks GPT to summarise." |
| 1:40 | The four step cards while they fill in | "Step 1 reads the question, step 2 is the vector search, step 3 the data it will use, step 4 the answer." |
| 2:00 | Answer, then click a `[S#]` | "Each sentence cites a source. Clicking it jumps to the exact statistic or headline." |
| 2:20 | Ask "What is the exact domestic market size of Brazil's dental market?" | "When the data cannot answer, it says so." |
| 2:35 | `eval/RESULTS.md` or the README evaluation section | "I measured this on 28 questions: number match rate, citation accuracy, retrieval hit rate." (only after you have run `python eval/run_eval.py`) |
| 2:50 | README limitations | "Import value is not market size, statistics lag, and news is noisy. These limits are written down." |

## Tips

* Say the limits out loud once. It reads as research judgement, not weakness.
* Do not show your API key or the terminal with the key typed in.
* Upload the video to YouTube as unlisted or commit it to `docs/` and link it at the top of the README.

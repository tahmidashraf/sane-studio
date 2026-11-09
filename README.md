# SANE Studio (browser version)

SANE Studio neutralizes subject-level EEG biomarkers, such as an elevated theta/beta ratio,
while keeping trial-level brain dynamics, events and auxiliary channels. It reads and writes
EDF, BDF, BrainVision, EEGLAB and FIF, and returns each recording in the format it arrived in.

This version runs entirely in the visitor's browser: the Python engine (MNE-Python, NumPy,
SciPy) runs in a Web Worker with [Pyodide](https://pyodide.org), so recordings never leave the
computer and the site needs no server. It is hosted as a static website on GitHub Pages; see
[DEPLOY.md](DEPLOY.md) for every step.

The site opens on the project page (`index.html`), which presents the SANE paper, its results
and figures. Its **Open SANE Studio** button leads to the app itself (`studio.html`).

## Layout

| Path | What it is |
|---|---|
| `index.html`, `static/img/` | the project page: abstract, method, results, figures from the paper, citation |
| `studio.html`, `css/`, `js/app.js`, `js/viewer.js`, `js/charts.js` | the SANE Studio user interface (same as the server version) |
| `js/api.js` | talks to the engine worker instead of a web server |
| `js/worker.js` | loads Pyodide and the Python packages, runs each request |
| `py/app/` | the SANE engine, file formats, analysis and job pipeline, unchanged from the server version |
| `py/browser_api.py` | the server's endpoints as functions; in-memory work arrays; progress to the page |
| `samples/` | two small public sample datasets |
| `manual.html`, `manual/` | the user manual |

## Run it on this PC

From this folder:

```bash
python -m http.server 8000
```

Then open <http://localhost:8000> for the project page, or <http://localhost:8000/studio.html>
for the app. (Opening `studio.html` directly from the disk does not work: browsers only start the
engine worker from a web address.)

## Server version

The server version (FastAPI, for very large files) is on the
[`server` branch](https://github.com/tahmidashraf/sane-studio/tree/server) of this repository,
with its Docker image and deployment files. On the same input, both versions give the same
output: on a sample EDF, 9 of 992,460 samples differ by one digital unit, from
floating-point rounding in different math libraries.

## License

The code is released under the [MIT License](LICENSE). The recordings in `samples/depression-rest` come from OpenNeuro ds003478 and stay under its CC0 terms.

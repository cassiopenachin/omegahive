# OmegaHive whitepaper

*OmegaHive: A Running Hive — architecture, operations, and the path from one operator to many hives.*
LaTeX source; `omegahive-a-running-hive.pdf` is the built artifact.

Build (needs texlive with tikz, tcolorbox, charter):

```
latexmk -pdf -interaction=nonstopmode main.tex && cp main.pdf omegahive-a-running-hive.pdf
```

Before a release build, refresh the task counts from the live board/metrics — the intro
paragraph and its footnote in `front.tex`, and the Sources appendix in `appendix.tex`.
Every number in the document is meant to be re-derivable from the spine at build date.

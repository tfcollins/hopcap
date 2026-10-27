"""Sphinx configuration for hopcap (Diátaxis layout, -W in CI)."""
import json
from pathlib import Path

project = "hopcap"
author = "Travis F. Collins"
release = "0.1.0"
extensions = ["sphinx.ext.githubpages", "myst_parser", "sphinxcontrib.mermaid", "adi_doctools"]
# Diagrams render at their natural size, shrunk only to fit the column (no fixed 500 px box that
# scaled the sequence diagram's labels to ~7 px); long sequence messages wrap between lifelines.
mermaid_init_config = {
    "startOnLoad": False,
    "deterministicIds": True,
    "flowchart": {"useMaxWidth": False},
    "sequence": {"useMaxWidth": False, "wrap": True},
    "state": {"useMaxWidth": False},
}
mermaid_height = "auto"
needs_extensions = {"adi_doctools": "0.4.21"}
html_theme = "cosmic"
html_static_path = ["_static"]
html_css_files = ["mermaid.css"]
exclude_patterns = ["_build", "superpowers"]
myst_enable_extensions = ["colon_fence", "deflist", "fieldlist"]
myst_heading_anchors = 3

# Render the register map from the single source of truth so docs cannot drift.
_regmap = json.loads((Path(__file__).resolve().parents[1] / "hdl" / "hopctrl_regmap.json").read_text())
_rows = ["| Offset | Name | Access | Notes |", "|---|---|---|---|"]
for r in _regmap["registers"]:
    note = r.get("desc") or r.get("bits") or ""
    if "write_bits" in r:
        note = "W: " + "; ".join(f"[{k}] {v}" for k, v in r["write_bits"].items()) + ". R: " + "; ".join(
            f"[{k}] {v}" for k, v in r["read_bits"].items())
    _rows.append(f"| `{r['offset']}` | `{r['name']}` | {r['access']} | {note} |")
(Path(__file__).resolve().parent / "reference" / "_regmap_table.md").write_text("\n".join(_rows) + "\n")

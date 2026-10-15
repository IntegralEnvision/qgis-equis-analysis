# Security

## Reporting a problem

Please report security problems privately, not in a public issue. Use
GitHub's **Report a vulnerability** button on this repository's
[Security tab](https://github.com/integralenvision/qgis-equis-analysis/security).
It opens a private advisory that only the maintainers can see.

Include the plugin version and enough detail to reproduce the problem. Do
not include a working API key or data from a real site.

## What is in scope

The plugin holds an EQuIS API key and talks to an EQuIS Enterprise site on
the user's behalf, so the things most worth reporting are:

- an API key leaving the machine anywhere except the configured EQuIS site,
  or appearing in a log, a layer, a project file or a release ZIP;
- the plugin writing to an EQuIS site while **Read-only** is on, or writing
  anything beyond what the README's "What it writes to your EQuIS site"
  section describes;
- cached or exported data being readable by other users of the machine
  where it should not be, or being shown as live when it is not.

Problems in EQuIS Enterprise itself belong with EarthSoft, not here.

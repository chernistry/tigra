# Changelog

All notable changes are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- `tiegraph build` turns a LinkedIn data export (the ZIP or its unpacked folder) into one offline HTML page.
- Five views: Constellations, Companies, Interaction radar, Timeline and Cohorts, with search, filters,
  growth playback, PNG export, a table view and a details panel.
- Inferred ties between contacts (same employer, similar title), Louvain communities, PageRank and
  betweenness.
- Time structure: growth regimes (CUSUM changepoints), connection cohorts (exact binomial scan with
  Benjamini-Hochberg), contacts you messaged in the same weeks (hypergeometric test with
  Benjamini-Yekutieli), and conversation responsiveness.
- A synthetic export generator for tests, demos and screenshots.

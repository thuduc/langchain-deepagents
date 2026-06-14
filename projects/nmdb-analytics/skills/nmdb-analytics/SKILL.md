---
name: nmdb-analytics
description: Explore and analyze the FHFA National Mortgage Database (NMDB) aggregate mortgage statistics CSV datasets in this repository. Use when users ask for analytics, summaries, comparisons, trends, charts, or data exploration involving NMDB new mortgage originations, outstanding mortgages, mortgage performance, series IDs, VALUE1/VALUE2 weighting, geographies, markets, periods, or suppression flags.
---

# NMDB Analytics

This skill covers the FHFA National Mortgage Database (NMDB) aggregate mortgage statistics files in this repository.

## Data Sources

- `../../data/nmdb-new-mortgage-statistics-national-census-areas-quarterly.csv`: quarterly new residential mortgage origination statistics.
- `../../data/nmdb-outstanding-mortgage-statistics-national-census-areas-quarterly.csv`: quarterly outstanding residential mortgage stock statistics.
- `../../data/nmdb-mortgage-performance-statistics-national-census-areas-quarterly.csv`: quarterly residential mortgage performance statistics.
- `../../data/DATA_DICTIONARY.md`: dataset schema, series IDs, market definitions, geography codes, units, weighting, suppression rules, and technical notes.
- `../../data/nmdb-aggregate-statistics-data-dictionary-technical-notes.pdf`: FHFA source document used to compile the data dictionary.

Read `../../data/DATA_DICTIONARY.md` whenever a prompt depends on field meanings, units, available series, market or geography definitions, suppression handling, VALUE1/VALUE2 interpretation, or dataset coverage.

## Data Model

- The three CSVs are long-format time series. `SERIESID` identifies the statistic, and `VALUE1` and, where present, `VALUE2` contain the measure values.
- The shared dimensions are `SOURCE`, `FREQUENCY`, `GEOLEVEL`, `GEOID`, `GEONAME`, `MARKET`, `PERIOD`, `YEAR`, `QUARTER`, `MONTH`, `SUPPRESSED`, and `SERIESID`.
- `VALUE1` is the count-weighted value. For new mortgage and outstanding mortgage files, `VALUE2` is the dollar-weighted value when applicable.
- The mortgage performance file has only `VALUE1`; it does not contain `VALUE2`.
- The current CSVs are quarterly national/census-area extracts with National, Rural/Non-Rural, Census Region, and Census Division geographies.
- `SUPPRESSED` is `1` when FHFA suppressed a value because the aggregate cell is based on fewer than 3 loans; `0` means not suppressed.

## Technical Context

- NMDB is a de-identified 1-in-20 random sample of closed-end first-lien residential mortgages reported to a national credit bureau.
- Investor mortgages and manufactured-home mortgages are less well represented than owner-occupied, site-built homes.
- New mortgage statistics are origination flow measures based on the account opening date.
- Outstanding mortgage statistics are stock measures of active mortgages open at the end of each quarter.
- Performance statistics reflect the last month of each quarter and apply to active loans from the quarter after origination through the quarter before termination.
- The forbearance performance series begins in `2019Q4`.
- The latest two quarters of performance statistics should be treated as preliminary because credit bureau reporting of new mortgages can lag by up to 6 months.
- If code execution is needed, use the repository Python virtual environment named `venv`.

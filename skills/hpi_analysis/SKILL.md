---
name: hpi_analysis
description: Explore and analyze the Federal Housing Finance Agency (FHFA) House Price Index (HPI) data using Python, pandas, and matplotlib.
---

# HPI Analysis Skill

This skill guides you on how to explore, filter, analyze, and visualize the Federal Housing Finance Agency (FHFA) House Price Index (HPI) data. Since this skill uses a data-centric metadata approach, you will write and execute Python/pandas scripts dynamically on the fly to answer the user's questions.

## Data Locations

- **Master CSV**: `data/hpi_master.csv` (contains over 180,000 observations of HPI data from 1975 to 2026).
- **Metadata Reference**: `skills/hpi_analysis/references/hpi_metadata.json` (describes columns, value types, and definitions).
- **Excel Dictionary**: `data/hpi_dictionary.xlsx` (original Excel schema source).

---

## 1. Schema & Column Quirks

Before writing any analysis scripts, review these column mappings:

1. **CSV vs. Dictionary Column Discrepancy**:
   - The Excel Dictionary sheet lists the 11th column as `Median Price ($)`.
   - **However, in the CSV file (`data/hpi_master.csv`), the columns are**:
     - `index_nsa`: Non-seasonally adjusted index (Numeric)
     - `index_sa`: Seasonally adjusted index (Numeric, contains nulls)
     - `rstderr`: Standard error (Numeric, optional)
     - `note`: Notes (String, optional)
   - The HPI master CSV **does not** contain the `Median Price ($)` column. Do not try to access it.
2. **Data Type Warning**:
   - The `note` column contains mixed data types (numbers and text). When loading the CSV file, use `low_memory=False` or specify data types to avoid pandas warnings:
     ```python
     df = pd.read_csv('data/hpi_master.csv', dtype={'note': str, 'place_id': str}, low_memory=False)
     ```

---

## 2. Geography Levels and Filtering

The dataset contains indices at different geographic granularities. You must filter by `level` to prevent aggregate/double-counting errors:

- **USA or Census Division**: Contains Census Division aggregates (e.g. `place_name = 'East North Central Division'`, `place_id = 'DV_ENC'`) and the national aggregate (`place_name = 'United States'`, `place_id = 'USA'`).
- **State**: Individual US states (e.g., `place_name = 'California'`, `place_id = 'CA'`).
- **MSA**: Metropolitan Statistical Areas (e.g., `place_name = 'Abilene, TX'`, `place_id = '10180'`).
- **Puerto Rico**: Separate territorial HPI entries.

> [!CRITICAL]
> **Never mix or average across levels** (e.g., do not calculate average HPI of the USA by averaging states and divisions together). Always filter by `level` first.

---

## 3. Data Availability & Selection

- **Frequency**:
  - `monthly`: Only available at the national or Census Division level (e.g. `level = 'USA or Census Division'`).
  - `quarterly`: Available for States, MSAs, and Puerto Rico, as well as Census Divisions and USA.
- **Indices**:
  - `index_nsa` (Non-Seasonally Adjusted) is the most complete index, populated for almost all rows.
  - `index_sa` (Seasonally Adjusted) is only populated for subset monthly and quarterly data. If a user asks for general trends, default to `index_nsa` but specify which one is used.

---

## 4. Environment & Execution Guidelines (Virtual Environment)

To maintain a clean and isolated system environment, you **must** always use a Python virtual environment when executing scripts or installing dependencies:

1. **Virtual Environment Location**: Use the virtual environment named `venv` located at the project root directory (`./venv`). If it does not exist, initialize it:
   ```bash
   python3 -m venv venv
   ```
2. **Package Installation**: Never install packages globally. Always install them via the virtual environment's pip binary:
   ```bash
   ./venv/bin/pip install <package-name>
   ```
3. **Script Execution**: Always run Python scripts using the virtual environment's python binary:
   ```bash
   ./venv/bin/python <script-path>
   ```

---

## 5. Verification Checklist

When the user asks you to explore or analyze the data:
1. Verify the exact geographic `level` they want to investigate.
2. Confirm the `hpi_flavor` and `hpi_type` columns make sense for the question (traditional purchase-only is standard).
3. If writing code dynamically:
   - Always run the script from the command line and verify the printed results.
   - Keep console outputs tabular and concise.
   - If plotting, output the path of the saved PNG/JPG file clearly, and present the image if the environment allows.

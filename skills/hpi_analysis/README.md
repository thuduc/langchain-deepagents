# Testing the HPI Analysis Skill in Codex

This directory contains the `hpi_analysis` agent skill. Since the skill is fully data-centric and metadata-driven, it does not use static code scripts. Instead, it provides files location, schemas, data quirks, and virtual environment instructions to the LLM agent, allowing it to write and run its own code on the fly.

## How to Install in a New Project

To use this HPI analysis skill in a brand new project, follow these exact steps:

1. **Set Up the Directory Structure**:
   In your new project root folder, ensure you have `data/` and `skills/` directories.

2. **Copy the Datasets**:
   Place the HPI dataset files in the `data/` directory:
   - `data/hpi_master.csv`
   - `data/hpi_dictionary.xlsx`

3. **Copy the Skill Configuration**:
   Copy the entire `hpi_analysis` skill folder into the `skills/` directory of your new project:
   - Destination path: `skills/hpi_analysis/` (must contain `SKILL.md` and the `references/hpi_metadata.json` metadata file).

4. **Verify the Layout**:
   Your new project folder should look like this:
   ```text
   new-project/
   ├── data/
   │   ├── hpi_master.csv
   │   └── hpi_dictionary.xlsx
   └── skills/
       └── hpi_analysis/
           ├── SKILL.md
           ├── README.md
           └── references/
               └── hpi_metadata.json
   ```

5. **Start Codex**:
   Open a terminal, navigate to your new project root folder, and run your Codex command. Codex will scan the `skills/` folder, automatically discover `skills/hpi_analysis/SKILL.md`, and load the skill rules.

6. **Let Codex Handle the REST**:
   When you send a prompt requesting HPI data analysis, Codex will automatically read the skill directives, initialize the local virtual environment (`venv`) if it does not exist, install the required packages (like `pandas` or `matplotlib`), run the dynamic code, and output the analysis.

---

## Example Prompts to Use

Here are typical prompts categorized by complexity that you can feed into the Codex harness to test the skill:

### 1. Data Exploration & Schema Questions
These test whether the agent reads the schema and column details correctly:
* `"Inspect the HPI dictionary and describe the difference between the geography levels: 'State' and 'USA or Census Division'."`
* `"Check the HPI master dataset to see if the index contains seasonally adjusted values for the MSA level."`
* `"What are the data types and column names present in data/hpi_master.csv? Does it align exactly with the excel dictionary?"` (This tests if it spots the column mismatch quirk documented in the skill).

### 2. Analytical Calculations
These test the agent's ability to write pandas filtering and aggregate logic:
* `"Calculate the housing price index growth rate in California (CA) between Q1 2012 and Q4 2022. Use the non-seasonally adjusted index."`
* `"Find the top 5 MSAs (Metropolitan Statistical Areas) that experienced the highest home price index growth between 2020 and 2025."`
* `"Compare the monthly HPI trend for the 'East North Central Division' against the 'Pacific Division' for the year 2024."`

### 3. Visualizations
These test the agent's ability to set up the virtual environment, install matplotlib/seaborn if needed, and output high-quality plots:
* `"Generate a line chart comparing the quarterly HPI trend from 2010 to 2025 for Texas (TX), Florida (FL), and New York (NY). Save the output as a PNG file in the examples directory."`
* `"Plot a bar chart showing the HPI growth rates of all West Coast states (CA, OR, WA) from Q1 2020 to Q1 2025. Save the output to the examples directory."`

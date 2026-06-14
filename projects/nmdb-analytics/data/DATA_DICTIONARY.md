# NMDB Aggregate Mortgage Statistics Data Dictionary

Source document: `nmdb-aggregate-statistics-data-dictionary-technical-notes.pdf`, FHFA National Mortgage Database Aggregate Mortgage Statistics Data Dictionary and Technical Notes, March 27, 2026.

This dictionary covers the three quarterly national/census-area CSV files in this repository. The source PDF also describes state, metro-area, annual, and monthly products; those products are not present here.

## Contents

- [Files Covered](#files-covered)
- [Data Model](#data-model)
- [Common Fields](#common-fields)
- [Value Columns and Weighting](#value-columns-and-weighting)
- [Dataset 1: New Residential Mortgage Statistics](#dataset-1-new-residential-mortgage-statistics)
- [Dataset 2: Outstanding Residential Mortgage Statistics](#dataset-2-outstanding-residential-mortgage-statistics)
- [Dataset 3: Residential Mortgage Performance Statistics](#dataset-3-residential-mortgage-performance-statistics)
- [Market Definitions](#market-definitions)
- [Geography Definitions](#geography-definitions)
- [Technical Notes](#technical-notes)

## Files Covered

| File | Description | Observed rows | Observed period coverage |
| --- | --- | ---: | --- |
| `nmdb-new-mortgage-statistics-national-census-areas-quarterly.csv` | New residential mortgage origination flow statistics for national, rural/non-rural, census region, and census division geographies. | 2,851,200 | `1998Q1` through `2025Q2` |
| `nmdb-outstanding-mortgage-statistics-national-census-areas-quarterly.csv` | Outstanding residential mortgage stock statistics for active mortgages as of the end of each quarter. | 123,136 | `2013Q1` through `2025Q4` |
| `nmdb-mortgage-performance-statistics-national-census-areas-quarterly.csv` | Residential mortgage performance rates for active mortgages as of the end of each quarter. | 24,576 | `2002Q1` through `2025Q4`; `PFORB` starts in `2019Q4` |

All three files contain `SOURCE = NMDB` and `FREQUENCY = Quarterly`.

## Data Model

The datasets are long-format aggregate time series. A row is a single statistic for one geography, market, period, and series ID.

Primary dimensional fields:

- `GEOLEVEL`, `GEOID`, `GEONAME`
- `MARKET`
- `PERIOD`, `YEAR`, `QUARTER`, `MONTH`
- `SERIESID`

Measure fields:

- `VALUE1`: count-weighted value.
- `VALUE2`: dollar-weighted value, present only in the new mortgage and outstanding mortgage files.

Use the CSV headers as authoritative for column order. The PDF presents the conceptual field positions, but the CSVs place `SERIESID` after `SUPPRESSED`.

Percent series are expressed as percentage points, not proportions. For example, `2.3` means 2.3 percent.

## Common Fields

| Column | Type | Description | Values or notes |
| --- | --- | --- | --- |
| `SOURCE` | String | Data source. | Always `NMDB` in these files. |
| `FREQUENCY` | String | Frequency of the series. | Always `Quarterly` in these files. |
| `GEOLEVEL` | String | Level of geography. | `National`, `Rural/Non-Rural`, `Census Region`, `Census Division`. |
| `GEOID` | String | Geography identifier. | See [Geography Definitions](#geography-definitions). |
| `GEONAME` | String | Geography name. | See [Geography Definitions](#geography-definitions). |
| `MARKET` | String | Mortgage market or submarket. | See [Market Definitions](#market-definitions). |
| `PERIOD` | String | Time period. | Quarterly period such as `2009Q2`. |
| `YEAR` | Numeric | Calendar year. | Four-digit year. |
| `QUARTER` | Numeric | Calendar quarter. | `1`, `2`, `3`, or `4`. |
| `MONTH` | String or numeric | Quarter-ending month. | March, June, September, December. Performance uses zero-padded values (`03`, `06`, `09`, `12`); new and outstanding files use `3`, `6`, `9`, `12`. |
| `SUPPRESSED` | Numeric | Suppression indicator. | `1` = suppressed; `0` = not suppressed. FHFA suppresses aggregate statistics based on fewer than 3 loans. |
| `SERIESID` | String | Statistic identifier. | See the dataset-specific series tables below. |
| `VALUE1` | Numeric | Count-weighted statistic value. | Present in all three datasets. |
| `VALUE2` | Numeric | Dollar-weighted statistic value. | Present in new mortgage and outstanding mortgage files only; absent from the performance file. Blank when not applicable. |

## Value Columns and Weighting

`VALUE1` and `VALUE2` are alternate weighting bases, not separate dimensions.

| Dataset | `VALUE1` | `VALUE2` |
| --- | --- | --- |
| New residential mortgage statistics | Weighted by number of mortgage originations. Each sample mortgage represents 20 actual mortgages. | Weighted by origination loan amount when applicable. |
| Outstanding residential mortgage statistics | Weighted by number of active mortgages at the end of the quarter. | Weighted by unpaid principal balance (UPB) at the end of the quarter when applicable. |
| Residential mortgage performance statistics | Rate among active loans, count-weighted. | Not present. |

Series IDs whose PDF notes say `VALUE1 only` are aggregate or count-style measures; do not interpret a populated duplicate `VALUE2` value for those rows as a distinct dollar-weighted statistic.

## Dataset 1: New Residential Mortgage Statistics

File: `nmdb-new-mortgage-statistics-national-census-areas-quarterly.csv`

New originations are mortgage loans initially funded during the period, identified by account opening date. They are categorized as home purchase mortgages or refinance mortgages.

Coverage in this repository:

- Periods: `1998Q1` through `2025Q2`.
- Geographies: National, Rural/Non-Rural, Census Region, Census Division.
- Markets: 27 market/submarket values, including home-purchase and refinance variants.
- Columns: common fields plus `VALUE1` and `VALUE2`.

### New Mortgage Series IDs

| `SERIESID` | Description | Units or notes |
| --- | --- | --- |
| `TOT_ORIG` | Number of originations. | Thousands of originations; `VALUE1` only. |
| `TOT_LOANAMT` | Origination volume. | Millions of dollars. |
| `AVE_LOANAMT` | Average loan amount. | Thousands of dollars, rounded to nearest $1,000; `VALUE1` only. |
| `AVE_PAYMENT` | Average initial required loan payment after origination. | Dollars, rounded to nearest dollar; `VALUE1` only. |
| `AVE_PROPVAL` | Average purchase price or appraised value. | Thousands of dollars, rounded to nearest $1,000; `VALUE1` only. |
| `AVE_INTRATE` | Average interest rate. | Contract interest rate at origination. |
| `PCT_OWNOCC` | Percent share of originations where the property is owner-occupied. | Percent. |
| `PCT_FTHB` | Percent share of home purchase originations that are first-time homebuyer loans. | Loan is first-time homebuyer when any borrower has no prior mortgage in the credit data. |
| `PCT_REPEATHB` | Percent share of home purchase originations that are repeat homebuyer loans. | None of the borrowers is a first-time homebuyer. |
| `PCT_HP` | Percent share of all originations that are home purchase loans. | Loan purpose is home purchase. |
| `PCT_CASHOUT` | Percent share of refinance originations that are cashout refinance loans. | New first lien plus any second lien exceeds prior first and second lien UPB by more than 5 percent. |
| `PCT_OTH_REFI` | Percent share of refinance originations that are rate-and-term refinance loans. | Refinance to take advantage of rates and/or term rather than cashout. |
| `PCT_REFI` | Percent share of all originations that are refinance loans. | Loan purpose is refinance. |
| `AVE_TERM` | Average term to maturity. | Years at origination. |
| `PCT_ARM` | Percent share that are adjustable-rate mortgages. | Percent. |
| `PCT_TERM_FRM_15` | Percent share that are fixed-rate mortgages with term less than or equal to 15 years. | Percent. |
| `PCT_TERM_FRM_30` | Percent share that are fixed-rate mortgages with term greater than 15 years. | Primarily 20-year and 30-year fixed-rate mortgages. |
| `AVE_DTI` | Average back-end debt-to-income ratio. | Percent. |
| `PCT_DTI_LE36` | Percent share with DTI less than or equal to 36 percent. | Percent. |
| `PCT_DTI_3743` | Percent share with DTI from 36.1 percent to 43 percent. | Percent. |
| `PCT_DTI_GE44` | Percent share with DTI greater than 43 percent. | Percent. |
| `AVE_VANTAGESCR` | Average borrower credit score. | Average VantageScore Version 3.0 across borrowers. |
| `PCT_VS_VERYPOOR` | Percent share with very poor credit. | Average borrower VantageScore 300-499. |
| `PCT_VS_POOR` | Percent share with poor credit. | Average borrower VantageScore 500-600. |
| `PCT_VS_FAIR` | Percent share with fair credit. | Average borrower VantageScore 601-660. |
| `PCT_VS_GOOD` | Percent share with good credit. | Average borrower VantageScore 661-780. |
| `PCT_VS_EXCELLENT` | Percent share with excellent credit. | Average borrower VantageScore 781-850. |
| `AVE_LTV` | Average loan-to-value ratio. | Based on first lien mortgage. |
| `AVE_CLTV` | Average combined loan-to-value ratio. | Based on first lien and contemporaneous subordinate liens. |
| `PCT_CLTV_LE70` | Percent share with CLTV less than or equal to 70 percent. | Percent. |
| `PCT_CLTV_7080` | Percent share with CLTV from 70.1 percent to 80.0 percent. | Percent. |
| `PCT_CLTV_8090` | Percent share with CLTV from 80.1 percent to 90.0 percent. | Percent. |
| `PCT_CLTV_9095` | Percent share with CLTV from 90.1 percent to 95.0 percent. | Percent. |
| `PCT_CLTV_9597` | Percent share with CLTV from 95.1 percent to 97.0 percent. | Percent. |
| `PCT_CLTV_GT97` | Percent share with CLTV greater than 97 percent. | Percent. |
| `PCT_GOVERNMENT` | Percent market share for government insured, guaranteed, or direct loans. | FHA, VA, and USDA RHS loans. |
| `PCT_ENTERPRISE` | Percent market share for Enterprise acquired loans. | Non-government loans acquired by Fannie Mae or Freddie Mac. |
| `PCT_OTHERCONFORMING` | Percent market share for other conforming loans. | Non-government, non-Enterprise conforming loans. |
| `PCT_NONCONFORMING` | Percent market share for jumbo loans. | Loans above the conforming loan limit. |
| `PCT_WHT` | Percent share where all borrowers are White alone. | Race category. |
| `PCT_BLK` | Percent share where all borrowers are Black or African American alone. | Race category. |
| `PCT_ASN` | Percent share where all borrowers are Asian alone. | Race category. |
| `PCT_HPI` | Percent share where all borrowers are Native Hawaiian or Other Pacific Islander alone. | Race category. |
| `PCT_AMI` | Percent share where all borrowers are American Indian or Alaska Native alone. | Race category. |
| `PCT_MIX` | Percent share with multiple races. | One or more borrowers reported more than one race, or borrowers are from different races. |
| `PCT_HIS` | Percent share where all borrowers are Hispanic or Latino. | Ethnicity category; borrowers may be of any race. |
| `PCT_HSP` | Percent share where at least one borrower is Hispanic or Latino. | Applies among loans with two or more borrowers; borrowers may be of any race. |
| `PCT_WNH` | Percent share where all borrowers are White alone and not Hispanic or Latino. | Race/ethnicity category. |
| `PCT_MNH` | Percent share with multiple races or race other than White, not Hispanic or Latino. | All borrowers are not Hispanic or Latino and either one or more borrowers reported more than one race, borrowers are from different races, or all borrowers are races other than White. |
| `AVE_AGE_BORROWER` | Average borrower age. | Average age across borrowers. |
| `PCT_AGE_LT25` | Percent share with average borrower age less than 25. | Percent. |
| `PCT_AGE_2534` | Percent share with average borrower age 25 to 34. | Percent. |
| `PCT_AGE_3544` | Percent share with average borrower age 35 to 44. | Percent. |
| `PCT_AGE_4554` | Percent share with average borrower age 45 to 54. | Percent. |
| `PCT_AGE_5564` | Percent share with average borrower age 55 to 64. | Percent. |
| `PCT_AGE_GE65` | Percent share with average borrower age at least 65. | Percent. |
| `PCT_MALEBOR` | Percent share with a single male borrower. | One borrower, and that borrower is male. |
| `PCT_FEMALEBOR` | Percent share with a single female borrower. | One borrower, and that borrower is female. |
| `PCT_TWOBOR` | Percent share with two borrowers. | Percent. |
| `PCT_MULTIBOR` | Percent share with more than two borrowers. | Percent. |

## Dataset 2: Outstanding Residential Mortgage Statistics

File: `nmdb-outstanding-mortgage-statistics-national-census-areas-quarterly.csv`

Outstanding mortgage statistics are stock measures of active mortgages open at the end of the quarter. Current active mortgages equal previous active mortgages plus new originations less mortgages paid off or otherwise terminated.

Coverage in this repository:

- Periods: `2013Q1` through `2025Q4`.
- Geographies: National, Rural/Non-Rural, Census Region, Census Division.
- Markets: `All Mortgages`, `Enterprise Acquisitions`, `Government / Non-Conventional`, `Other Conventional Market`.
- Columns: common fields plus `VALUE1` and `VALUE2`.

### Outstanding Mortgage Series IDs

| `SERIESID` | Description | Units or notes |
| --- | --- | --- |
| `TOT_LOANS` | Outstanding active loans. | Thousands of mortgages; `VALUE1` only. |
| `PCT_LOANS` | Percent share of active loans by geography. | Active loans in the geography divided by total nationwide active loans; `VALUE1` only. |
| `TOT_UPB` | Outstanding active unpaid principal balance. | Billions of dollars. |
| `PCT_UPB` | Percent share of UPB by geography. | UPB in the geography divided by total nationwide UPB. |
| `TOT_LOANAMT` | Origination loan volume for active mortgages. | Billions of dollars; `VALUE1` only in the PDF notes. |
| `AVE_PAYMENT` | Average monthly payment. | Principal, interest, and escrow where applicable for the month ending the quarter; `VALUE1` only. |
| `AVE_MTMLTV` | Average mark-to-market loan-to-value ratio. | See formula in [Technical Notes](#technical-notes). |
| `PCT_MTMLTV_LE60` | Percent share with mark-to-market LTV less than or equal to 60 percent. | Percent. |
| `PCT_MTMLTV_61_70` | Percent share with mark-to-market LTV from 60.1 percent to 70.0 percent. | Percent. |
| `PCT_MTMLTV_71_80` | Percent share with mark-to-market LTV from 70.1 percent to 80.0 percent. | Percent. |
| `PCT_MTMLTV_81_90` | Percent share with mark-to-market LTV from 80.1 percent to 90.0 percent. | Percent. |
| `PCT_MTMLTV_91_100` | Percent share with mark-to-market LTV from 90.1 percent to 100 percent. | Percent. |
| `PCT_MTMLTV_GT100` | Percent share with mark-to-market LTV greater than 100 percent. | Percent. |
| `AVE_INTRATE` | Average contract interest rate at origination. | Percent. |
| `PCT_INTRATE_LT_3` | Percent share with contract interest rate below 3 percent. | Percent. |
| `PCT_INTRATE_3_4` | Percent share with contract interest rate from 3.00 percent to 3.99 percent. | Percent. |
| `PCT_INTRATE_4_5` | Percent share with contract interest rate from 4.00 percent to 4.99 percent. | Percent. |
| `PCT_INTRATE_5_6` | Percent share with contract interest rate from 5.00 percent to 5.99 percent. | Percent. |
| `PCT_INTRATE_GE_6` | Percent share with contract interest rate at or above 6 percent. | Percent. |
| `PCT_TERM_ARM_1_4` | Percent share of ARMs originated within the prior 4 years. | Adjustable-rate mortgages. |
| `PCT_TERM_ARM_5PL` | Percent share of ARMs originated more than 4 years ago. | Adjustable-rate mortgages. |
| `PCT_TERM_FRM_15` | Percent share of FRMs with term 15 years or less. | Primarily 15-year mortgages. |
| `PCT_TERM_FRM_30` | Percent share of FRMs with term greater than 15 years. | Primarily 20-year and 30-year mortgages. |
| `AVE_VANTAGESCR` | Average borrower credit score at current quarter. | Average VantageScore Version 3.0 across borrowers. |
| `PCT_VS_VERYPOOR` | Percent share with very poor credit. | Average borrower VantageScore 300-499. |
| `PCT_VS_POOR` | Percent share with poor credit. | Average borrower VantageScore 500-600. |
| `PCT_VS_FAIR` | Percent share with fair credit. | Average borrower VantageScore 601-660. |
| `PCT_VS_GOOD` | Percent share with good credit. | Average borrower VantageScore 661-780. |
| `PCT_VS_EXCELLENT` | Percent share with excellent credit. | Average borrower VantageScore 781-850. |
| `AVE_AGE_LOAN` | Average age of mortgage loan. | Average months since loan was originated. |
| `PCT_TENURE_1_4` | Percent share originated within 4 years. | Loan age less than or equal to 4 years. |
| `PCT_TENURE_5_7` | Percent share originated 4.01 to 7.00 years ago. | Percent. |
| `PCT_TENURE_8_10` | Percent share originated 7.01 to 10.00 years ago. | Percent. |
| `PCT_TENURE_11PL` | Percent share originated more than 10 years ago. | Percent. |
| `PCT_GOVERNMENT` | Percent market share for government insured, guaranteed, or direct loans. | CSV uses `PCT_GOVERNMENT`; the PDF table contains a typographic zero in this ID. |
| `PCT_ENTERPRISE` | Percent market share for Enterprise acquired loans. | Non-government loans acquired by an Enterprise. |
| `PCT_OTHER` | Percent market share for other conventional loans. | Non-government, non-Enterprise loans; includes jumbo. |

## Dataset 3: Residential Mortgage Performance Statistics

File: `nmdb-mortgage-performance-statistics-national-census-areas-quarterly.csv`

Performance statistics describe mutually exclusive performance categories for active loans as of the end of the quarter. The data reflect performance in the last month of each quarter. Performance and forbearance rates are calculated only on active loans from the quarter after origination through the quarter before termination.

Coverage in this repository:

- Periods: `2002Q1` through `2025Q4`.
- `PFORB` forbearance values begin in `2019Q4`; earlier `PFORB` rows have blank `VALUE1`.
- Geographies: National, Rural/Non-Rural, Census Region, Census Division.
- Markets: `All Mortgages`, `Enterprise Acquisitions`, `Government / Non-Conventional`, `Other Conventional Market`.
- Columns: common fields plus `VALUE1`; no `VALUE2`.

### Performance Series IDs

| `SERIESID` | Description | Units or notes |
| --- | --- | --- |
| `P3089DL` | Percent 30 or 60 days past due. | Active loans 30 or 60 days past due, subject to stale account rules, divided by all active loans. |
| `P90DL` | Percent 90 or more days past due. | Active loans at least 90, 120, 150, or 180 days past due, subject to stale account rules, divided by all active loans. |
| `PFORECL` | Percent in process of foreclosure, bankruptcy, or deed in lieu. | Active loans in these processes, subject to stale account rules, divided by all active loans. |
| `PFORB` | Percent in forbearance. | Active loans indicated as in forbearance, divided by all active loans; begins in `2019Q4`. |

## Market Definitions

The new mortgage dataset contains the full set of market and purpose variants. The outstanding and performance datasets in this repository contain only the four broad markets listed above.

### Base Markets

| Market | Definition |
| --- | --- |
| `All Mortgages` | All single-family mortgage originations from NMDB. Coverage includes all 50 states and the District of Columbia, based on a 5 percent sample of credit reports. |
| `Conventional Market` | Subset of all mortgages that are not government insured, guaranteed, or direct loans. |
| `Conforming Market` | Subset of all mortgages at or below the applicable FHFA conforming loan limit, adjusted for number of units. |
| `Conventional Conforming Market` | Non-government conventional loans at or below the applicable conforming loan limit. |
| `Enterprise Acquisitions` | Non-government loans acquired by Fannie Mae or Freddie Mac. |
| `Government / Non-Conventional` | Government insured, guaranteed, or direct loans, including FHA, VA, and USDA Rural Housing Service loans. |
| `Other Conventional Market` | Other conventional loans after government and Enterprise acquired loans are removed; includes Federal Home Loan Bank Acquired Member Assets, credit union loans, private label mortgage pools, other portfolio loans, and jumbo mortgages. |
| `Other Conforming Market` | Other conventional conforming loans after government, Enterprise acquired, and jumbo loans are removed; includes Federal Home Loan Bank Acquired Member Assets, credit union loans, and other portfolio loans. |
| `Jumbo Market` | Loans above the applicable FHFA conforming loan limit, adjusted for number of units. |

### Purpose Variants in the New Mortgage File

For most base markets, the new mortgage file includes:

- `(Home Purchase)`: subset where the loan purpose is home purchase.
- `(Refinance)`: subset where the loan purpose is refinance.

Exact `MARKET` values observed in the new mortgage file:

| Market value |
| --- |
| `All Mortgages` |
| `All Mortgages (Home Purchase)` |
| `All Mortgages (Refinance)` |
| `Conforming Market` |
| `Conforming Market (Home Purchase)` |
| `Conforming Market (Refinance)` |
| `Conventional Conforming Market` |
| `Conventional Conforming Market (Home Purchase)` |
| `Conventional Conforming Market (Refinance)` |
| `Conventional Market` |
| `Conventional Market (Home Purchase)` |
| `Conventional Market (Refinance)` |
| `Enterprise Acquisitions` |
| `Enterprise Acquisitions (Home Purchase)` |
| `Enterprise Acquisitions (Refinance)` |
| `Government / Non-Conventional` |
| `Government / Non-Conventional (Home Purchase)` |
| `Government / Non-Conventional (Refinance)` |
| `Jumbo Market` |
| `Jumbo Market (Home Purchase)` |
| `Jumbo Market (Refinance)` |
| `Other Conforming Market` |
| `Other Conforming Market (Home Purchase)` |
| `Other Conforming Market (Refinance)` |
| `Other Conventional Market` |
| `Other Conventional Market (Home Purchase)` |
| `Other Conventional Market (Refinance)` |

## Geography Definitions

The three files in this repository contain national, rural/non-rural, census region, and census division geography only.

| `GEOLEVEL` | `GEOID` | `GEONAME` | Definition or included states |
| --- | --- | --- | --- |
| `National` | `USA` | `United States` | 50 states and the District of Columbia. |
| `Rural/Non-Rural` | `USARA` | `United States Rural` | Census tracts defined as rural by FHFA Duty to Serve regulation using the 2025 FHFA data. |
| `Rural/Non-Rural` | `USANRA` | `United States Non-Rural` | Census tracts not classified as rural by FHFA Duty to Serve regulation. |
| `Census Region` | `RNE` | `Northeast` | New England and Middle Atlantic divisions. |
| `Census Region` | `RMW` | `Midwest` | East North Central and West North Central divisions. |
| `Census Region` | `RS` | `South` | South Atlantic, East South Central, and West South Central divisions. |
| `Census Region` | `RW` | `West` | Mountain and Pacific divisions. |
| `Census Division` | `DNE` | `New England` | Maine, New Hampshire, Vermont, Massachusetts, Rhode Island, Connecticut. |
| `Census Division` | `DMA` | `Middle Atlantic` | New York, New Jersey, Pennsylvania. |
| `Census Division` | `DENC` | `East North Central` | Ohio, Indiana, Illinois, Michigan, Wisconsin. |
| `Census Division` | `DWNC` | `West North Central` | Minnesota, Iowa, Missouri, North Dakota, South Dakota, Nebraska, Kansas. |
| `Census Division` | `DSA` | `South Atlantic` | Delaware, Maryland, District of Columbia, Virginia, West Virginia, North Carolina, South Carolina, Georgia, Florida. |
| `Census Division` | `DESC` | `East South Central` | Kentucky, Tennessee, Alabama, Mississippi. |
| `Census Division` | `DWSC` | `West South Central` | Arkansas, Louisiana, Oklahoma, Texas. |
| `Census Division` | `DMTN` | `Mountain` | Montana, Idaho, Wyoming, Colorado, New Mexico, Arizona, Utah, Nevada. |
| `Census Division` | `DPAC` | `Pacific` | Washington, Oregon, California, Alaska, Hawaii. |

## Technical Notes

NMDB is a de-identified loan-level database of closed-end first-lien residential mortgages. The core data are a statistically valid 1-in-20 random sample of closed-end first-lien mortgages active since January 1998 and reported to a national credit bureau. NMDB is representative of the residential mortgage market as a whole, but investor mortgages and manufactured-home mortgages are less well represented because some non-person borrower and manufactured-home lending records are not consistently reported or defined in credit bureau data.

Suppression:

- Aggregated statistics are suppressed when they are based on fewer than 3 loans.
- Suppressed rows are indicated by `SUPPRESSED = 1`.
- The current files in this repository contain `SUPPRESSED = 0` in observed rows, but downstream users should still check the flag.

Performance statistics:

- Active loans are mortgage loans that are not closed or terminated and have performance data.
- Recently originated loans may take months to appear in credit history; until a valid performance code is found, they are treated as current and included in the denominator.
- The stale account rule applies prior performance status when a current quarter-ending performance code is missing. Beginning in 2012, the rule looks back up to two months; before 2012, it looks back up to six months. A 24-month rule applies to loans 180 or more days past due or in foreclosure, bankruptcy, or deed in lieu.
- The latest two quarters of performance and forbearance statistics should be treated as preliminary because credit bureau reporting of new mortgages can lag by up to 6 months. Initial estimates of delinquency and forbearance are generally higher than revised values.
- Credit history may be suppressed when consumers dispute credit reports or when federally declared natural disasters affect a region and time period.
- Fair Credit Reporting Act purge rules can undercount seriously past due loans from 2006 to 2012, with the undercount diminishing closer to the end of 2012.

Mark-to-market LTV:

```text
mark-to-market LTV = current UPB / ((origination property value / origination HPI) * current HPI)
```

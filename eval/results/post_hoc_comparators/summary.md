# Post-hoc comparators from the frozen artifacts

Descriptive only; nothing here was prespecified. Source: `eval/post_hoc_comparators.py` over the per-county `result.json` files of the frozen Study 2 and Study 3 directories.

## study2_v3_rev2: 9 county-events, 10 episodes

| policy | pooled unobserved h (offset median; min to max) | episodes observed | acquisitions |
|---|---|---|---|
| hybrid | 9.00 | 9 | 740 |
| adaptive | 23.50 | 8 | 607 |
| fixed 1h | 4.12 (0.25 to 7.75) | 9.5 | 2251 | hybrid below median in 5, above in 4, ties 0; one-sided sign p = 0.500
| fixed 2h | 9.00 (0.75 to 17.75) | 9.0 | 1128 | hybrid below median in 6, above in 3, ties 0; one-sided sign p = 0.254
| fixed 3h | 14.25 (1.25 to 26.50) | 9.0 | 754 | hybrid below median in 6, above in 3, ties 0; one-sided sign p = 0.254
| fixed 6h | 28.38 (2.75 to 52.00) | 8.0 | 378 | hybrid below median in 9, above in 0, ties 0; one-sided sign p = 0.002
| fixed 12h | 55.38 (4.25 to 104.50) | 8.0 | 202 | hybrid below median in 9, above in 0, ties 0; one-sided sign p = 0.002

Anticipation strata (county-event level; a county-event is 'all' when every episode had an onset-anticipating warning):

| stratum | county-events | episodes | hybrid unobs h | fixed 3 h median unobs h | adaptive unobs h | episodes observed hybrid / fixed 3 h / adaptive |
|---|---|---|---|---|---|---|
| all_anticipated | 6 | 7 | 5.50 | 10.12 | 17.75 | 6 / 6.0 / 5 |
| none_anticipated | 3 | 3 | 3.50 | 4.12 | 5.75 | 3 / 3.0 / 3 |

## study3_v4_additive: 24 county-events, 46 episodes

| policy | pooled unobserved h (offset median; min to max) | episodes observed | acquisitions |
|---|---|---|---|
| hybrid | 60.25 | 33 | 2127 |
| adaptive | 109.50 | 32 | 1610 |
| fixed 1h | 24.88 (14.00 to 37.50) | 38.0 | 6787 | hybrid below median in 5, above in 19, ties 0; one-sided sign p = 0.999
| fixed 2h | 42.88 (18.75 to 69.50) | 36.5 | 3397 | hybrid below median in 14, above in 10, ties 0; one-sided sign p = 0.271
| fixed 3h | 60.62 (22.50 to 97.00) | 34.0 | 2268 | hybrid below median in 17, above in 7, ties 0; one-sided sign p = 0.032
| fixed 6h | 107.12 (30.25 to 180.75) | 30.0 | 1136 | hybrid below median in 21, above in 3, ties 0; one-sided sign p = 0.000
| fixed 12h | 188.50 (43.50 to 330.50) | 28.0 | 572 | hybrid below median in 24, above in 0, ties 0; one-sided sign p = 0.000

Anticipation strata (county-event level; a county-event is 'all' when every episode had an onset-anticipating warning):

| stratum | county-events | episodes | hybrid unobs h | fixed 3 h median unobs h | adaptive unobs h | episodes observed hybrid / fixed 3 h / adaptive |
|---|---|---|---|---|---|---|
| all_anticipated | 17 | 19 | 10.50 | 25.00 | 48.75 | 18 / 18.0 / 17 |
| some_anticipated | 3 | 9 | 24.75 | 20.38 | 33.75 | 7 / 7.0 / 7 |
| none_anticipated | 4 | 18 | 25.00 | 15.25 | 27.00 | 8 / 9.0 / 8 |
